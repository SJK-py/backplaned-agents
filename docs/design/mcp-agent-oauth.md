# OAuth for an MCP server's own credential (admin-managed)

> **Status: proposed.** No code. This is the *simple* half of MCP
> authorization, and it should ship before the hard half
> ([`mcp-per-user-oauth.md`](./mcp-per-user-oauth.md), deferred).
>
> The credential stays **one per MCP server** — one per agent — exactly as
> today. Nothing about identity, ACL, mode sets, or health changes. What
> changes is only how that one credential is *acquired and kept alive*: from
> "an operator pastes a long-lived token into an env var and redeploys" to
> "an operator clicks Connect in the admin UI and the router refreshes it."
>
> **Why this is not merely a convenience.** A growing number of hosted MCP
> servers only accept OAuth bearer tokens and will not accept the service's
> native API key at all — a Notion `ntn_` integration token authenticates
> against Notion's REST API and is rejected by its MCP endpoint. Without
> this, those servers cannot be connected at all. It closes a capability
> gap, not an ergonomics gap.
>
> §2 is the section that matters: it explains why per-*agent* OAuth has none
> of the architectural friction per-*user* credentials do.

## 1. Why

`mcp_servers.auth_value_ref` holds an `env://VAR` reference the **bridge
process** resolves from its own environment (`auth_resolver.py`). Three
consequences, in increasing order of severity:

1. **Rotating a credential means editing `deploy/.env.prod` and restarting a
   container.** There is no in-product path.
2. **An expiring credential breaks the server silently.** An `env://` value is
   static. A token with a 1-hour life is unusable; a token with a 90-day life
   fails on day 91 with no warning and no self-healing.
3. **Some MCP servers cannot be connected at all.** Where the provider's MCP
   endpoint requires an OAuth bearer token and will not take a PAT, the
   current model has no answer. This is the one that makes it a feature rather
   than a cleanup.

## 2. Why this has none of the per-user friction

The [per-user doc](./mcp-per-user-oauth.md) spends four subsections keeping a
per-user dimension out of the agent layer. **None of that applies here**, and
the reason is worth stating precisely because it is the whole argument for
doing this first.

**The platform's model is: an agent is a stateless workhorse; the router
enforces access control.** An agent does not decide who may call it, and it
does not carry per-caller policy — the ACL does, evaluated router-side, with
the result precomputed into the catalogue (`visibility.py`). Per-user
credentials ask the agent to hold a different credential per caller, which is
per-caller policy living inside the agent. That is an innate mismatch with the
architecture, not an implementation inconvenience, and it is why that doc
keeps needing another rule to hold the line.

Per-agent OAuth has **one credential per agent**, which is what the
architecture already expects. Concretely, everything that doc had to solve
simply does not arise:

| Per-user problem | Here |
| --- | --- |
| Per-`(server, user)` connection pool (§4.2) | One connection, as today |
| `health.py` decomposed into three questions (§4.4) | Unchanged — one credential, one connection, one gate |
| `user_id` cannot be a metric label (§4.4) | No per-user metrics exist |
| Offered tool list can't be filtered per user (§4.3) | Every user sees the same tools, correctly |
| Admit-time "has this user connected" check (§4.3) | None |
| Second give-up gate keyed by credential (§4.4) | None |
| Encryption at rest is a hard blocker (§3.3) | Downgraded — see §5 |

The residual is genuinely small: an OAuth client, four columns, a Connect
button, and a refresh loop.

## 3. Shape

### 3.1 `auth_kind = 'oauth'`

A fourth value on the existing CHECK pair (`mcp_servers_auth_kind_check` and
`mcp_servers_auth_consistent`), which currently admit `none` / `bearer` /
`header`. The `oauth` branch requires the registration fields of §3.2 and
leaves `auth_value_ref` NULL — the credential is not a reference to the
bridge's environment any more.

At the wire level `oauth` is identical to `bearer`: an `Authorization: Bearer
<token>` header. `_build_headers` (`mcp_client.py:263`) needs no new branch —
only a different source for the value.

### 3.2 Columns

Registration (operator-supplied, in the admin UI):

  * `oauth_issuer` — for discovery; or explicit `oauth_authorization_endpoint`
    / `oauth_token_endpoint` for providers without a discovery document.
  * `oauth_client_id`
  * `oauth_client_secret_ref` — a **reference** (`env://…`), not a literal,
    matching `auth_value_ref` and `code_agents.secret_refs`. Reuse the admin
    API's existing `_check_secret_refs` validator rather than writing a
    second one.
  * `oauth_scopes text[]`

Token state (router-managed, written by the OAuth flow and the refresh loop):

  * `oauth_access_token`, `oauth_refresh_token`, `oauth_expires_at`

**Storing token material in the row is a new thing for `mcp_servers`, but not
for this codebase.** `llm_presets` already carries both `api_key_ref` (the
indirection) *and* `api_key` (the literal), for exactly this reason: some
credentials cannot be an env var known at deploy time. `mcp_servers` follows
the established pattern rather than inventing one.

### 3.3 The router refreshes; the bridge just reads

The router is already the OIDC relying party and already owns
`security/oidc.py`. It refreshes proactively before `oauth_expires_at`, writes
the new token to the row, and the bridge picks it up on its next reconcile
poll — a path that already exists, because `AdminClient.list_mcp_servers()`
(`admin_client.py:167`) is polled every 30s.

**The access token must not be part of `config_signature()`.** That tuple
(`server_bridge.py:150`) is documented as "fields whose change requires a full
bridge restart" and includes `auth_value_ref`. If the token joined it, every
hourly refresh would restart the bridge and drop in-flight calls. The
supervisor already has the right path for a row that changed without
warranting a restart — the same one `pending_invitation_token` uses ("same
config signature — only rebuild the entry if the full row actually differs").

On a `401`/`403` from the upstream the bridge should re-read the row once
before surfacing an error, so a just-rotated token is picked up without
waiting for the poll.

### 3.4 Admin UI

`bp_admin/pages/mcp_servers.py` already has the form and the `auth_kind`
select (`AUTH_KIND_OPTIONS`, line 41). Additions: `oauth` in the select, the
§3.2 registration fields, a **Connect** button that starts the authorization
redirect, a **Disconnect** that clears token state and attempts provider-side
revocation, and a status badge (connected / expired / never connected).

`bp_admin` is a FastAPI app with its own `APIRouter` and session middleware,
so hosting the redirect callback is ordinary. The redirect URI must be
allowlisted the way `oidc_allowed_redirect_uris` already is
(`_check_oidc_redirect_uri`) — an open redirect here hands the operator's
authorization code to whoever asked.

Reuse the back-channel split the webapp already uses for OIDC login: the
router exposes `authorize` (returns the URL plus transient state/PKCE) and
`exchange`; `bp_admin` owns the browser redirect and holds the transient
state. Same shape, different caller.

## 4. What `security/oidc.py` still needs

Two gaps, unchanged from the per-user doc's §2.2 — this feature does not avoid
them, it just needs nothing *beyond* them:

  * **Generalize from a singleton to N providers.** It is constructed once at
    startup from `OIDC_*` settings (`app.py:414`) and hung on
    `app.state.bp.oidc_provider`. Per-server registration means resolving a
    provider per MCP server, with the discovery cache keyed accordingly.
  * **Add the refresh grant.** Only `exchange_code` exists today, because
    login never needs to refresh against the OP — the router issues its own
    `TokenPair` and forgets it. A long-lived service credential must refresh.

`id_token` validation does **not** transfer: an MCP server is a resource
server, and there may be no `id_token` at all.

## 5. Encryption at rest — recommended, no longer blocking

The per-user doc makes encryption at rest a hard prerequisite (§3.3) because
user-delegated tokens turn a database dump into account takeover across
accounts the operator cannot rotate. **That argument does not hold here**, and
it would be dishonest to reuse it for weight: the credential is the operator's
own, in the same risk class as `llm_presets.api_key` (already plaintext) and
as the PAT sitting in `deploy/.env.prod` today. A dump yields credentials the
operator can rotate themselves.

So: not a blocker. Still worth doing, for two reasons that are specific rather
than general — an OAuth **refresh** token is typically longer-lived and
broader-scoped than the PAT it replaces, and building the encrypted-column
machinery here is cheaper than building it later under per-user pressure,
where it *is* load-bearing.

## 6. Caveats

  * **The "service account" is usually a person.** Most providers issue OAuth
    tokens against a user account, so the connected identity is a real
    employee. When they leave, the MCP server breaks. Machine users / service
    accounts are the mitigation where the provider offers them; where it does
    not, this is a documented operational risk, not something the design can
    fix.
  * **Refresh tokens expire too.** Some providers expire them outright, some
    on inactivity, some rotate them on every use. When refresh fails
    permanently the server is down until an operator clicks Connect again —
    so this needs an **operator-visible signal**, not just a log line. A
    status badge in the admin UI plus a metric is the minimum; without it the
    failure is exactly as silent as the expiring-PAT problem this feature set
    out to fix.
  * **Concurrent refresh is a race, and the router being single-worker is the
    only thing hiding it.** Many providers invalidate the old refresh token
    when it is used. Two router workers refreshing the same row concurrently
    would have one of them invalidate the other's token and write a dead
    value. The router runs as a single worker today (`scaling.md` §1.1), so
    this is safe *now* — which makes it a landmine for the multi-worker work
    rather than a present bug. Guard it at write time (a row-level lock or a
    conditional update on `oauth_expires_at`) while it is cheap, and note it
    in `scaling.md` as one more thing single-worker is load-bearing for.
  * **Scope changes need re-consent.** A provider adding a required scope
    means Reconnect, not a config edit. The status badge should distinguish
    "expired" from "insufficient scope" if the provider says so.
  * **`stdio` servers are out of scope.** A local subprocess's credential is
    its spawned environment; `env_refs` already covers that and OAuth adds
    nothing. Refuse `oauth` for `transport = 'stdio'` at the admin API, the
    same way the per-user doc refuses `per_user` there.
  * **This does not give per-user identity.** Every user still acts as the one
    connected account. That is the deliberate scope; see §8.

## 7. Non-goals

  * **Per-user credentials.** [`mcp-per-user-oauth.md`](./mcp-per-user-oauth.md).
  * **Dynamic client registration** (RFC 7591). Operators register manually.
  * **Replacing `auth_kind` `bearer` / `header`.** A static PAT stays the right
    answer for a provider that issues one and for an internal service; this is
    a fourth option, not a migration.
  * **OAuth for `code_agents` secrets.** Different feature, same validator.

## 8. Relationship to per-user credentials

This is a **strict subset**, not a detour. The OAuth client generalization
(§4), the token columns and their encryption (§5), the refresh loop, the
redirect-URI allowlisting, and the admin Connect flow are all shared with the
per-user design. If per-user credentials are ever built, this is its phase 1
with none of the work discarded.

What per-user adds on top is exactly the part that fights the architecture:
a credential keyed by caller, which means a pool, a second give-up gate, a
health decomposition, and an admit-time check. Shipping this first means that
if per-user is never justified, nothing was wasted — and if it is, the
foundation is already load-bearing and proven.

## 9. What not to do

  * **Don't put the access token in `config_signature()`.** §3.3. Every
    refresh would restart the bridge and drop in-flight calls. Use the
    existing same-signature-row-differs path.
  * **Don't let the bridge refresh.** It would need the client secret, and the
    bridge is the process that also runs operator code with unrestricted
    egress (`bridge-python-code-agents.md` §3.1). The router refreshes; the
    bridge reads a token it cannot renew.
  * **Don't store `oauth_client_secret` as a literal.** It is a `_ref`, like
    `auth_value_ref` and `code_agents.secret_refs`. The token state is the
    thing that has to live in the row (§3.2); the client secret does not.
  * **Don't reuse the per-user doc's encryption argument to justify blocking
    on it.** §5. It is a different risk class and saying otherwise inflates a
    real-but-modest concern into a false prerequisite.
  * **Don't skip the operator-visible failure signal.** §6. A permanently
    failed refresh is silent, and silence is the exact defect this feature
    exists to remove.
  * **Don't ignore the concurrent-refresh race because the router is
    single-worker.** §6. Guard the write now, while it costs one conditional
    update, rather than discovering it during the multi-worker work.
  * **Don't let this become per-user by accident.** If a column, a cache key,
    or an endpoint in this feature acquires a `user_id`, stop: that is the
    other doc, with the other doc's constraints.
