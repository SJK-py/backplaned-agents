# Per-user credentials for MCP servers

> **Status: proposed.** No code. This doc settles two questions that must be
> answered before any is written: **who holds a user's third-party token**
> (§3) and **whether the credential is per-connection or per-call** (§4).
> Everything else follows from those.
>
> The prerequisites are unusually good — `ctx.user_id` already reaches the
> bridge handler, `bp_router/security/oidc.py` is already a working OAuth2
> client, and `user_llm_preferences` is already the precedent for the data
> model. The blocker is not the bridge. It is that **this repo has no
> encryption at rest** (§3.3), and user-delegated tokens are the wrong thing
> to be the first plaintext secret at scale.
>
> Phasing in §10: a **pasted bearer token** ships first and proves the whole
> per-user path; OAuth comes second.

## 1. Why

An MCP server today carries exactly one credential for every user of the
deployment. `mcp_servers.auth_value_ref` is an `env://VAR` reference resolved
from the **bridge process's** environment, so a GitHub MCP server is
*the operator's* GitHub, and every user's `github/create_issue` call acts as
that one identity.

That is right for a shared internal service and wrong for anything personal.
A user asking the assistant to check *their* Linear issues, read *their*
Drive, or open a PR as *themselves* needs the call to carry their own
authorization. The ask: let a user connect their own account to an MCP
server from the webapp, and have the bridge use that credential for tasks
belonging to that user.

**What this is not.** Not a second auth system for the platform — users
already authenticate to the router (password or OIDC, `oidc-webapp.md`).
This is *delegation* to a third party, which the router currently does
nowhere.

## 2. What already exists

Three findings, each verified in the tree, that decide the shape.

### 2.1 The bridge handler already knows whose task it is

`bp_sdk/context.py:73` — `TaskContext.user_id`. The router derives it from
the task row and never accepts it from the wire (this is the
"`user_id` is derived from the task, never asserted by an agent" property the
whole platform rests on). `tool_agent.py:194`'s handler already takes
`ctx`, so selecting a credential by user needs no new plumbing to *reach*
the decision point — only a store to consult.

Without this the feature would be impossible rather than merely expensive.
It is worth stating plainly because it is the single reason this is a
tractable change.

### 2.2 `bp_router/security/oidc.py` is a real OAuth2 client

277 lines implementing discovery, PKCE S256, `state`/`nonce` generation, the
authorization-code redirect URL, and code exchange. It was written for
login, but the protocol machinery is not login-specific.

Two gaps, both real work:

  * **It is a singleton for one OP.** Constructed once at startup from
    `OIDC_*` settings (`app.py:414`) and hung on `app.state.bp.oidc_provider`.
    Per-user MCP credentials need **N** providers — one per MCP server that
    opts in — resolved per request.
  * **There is no refresh grant.** Only `exchange_code`. Login doesn't need
    one (the router issues its own `TokenPair` after exchange and forgets the
    OP). A delegated third-party token must be refreshed for the life of the
    connection.

### 2.3 `user_llm_preferences` is the data-model precedent

`router-resolved-preset-slots.md` §4 already argued the exact question this
feature raises: where does a per-user value live when the router *acts* on
it? The answer there was a dedicated table rather than the session store's
user-scoped state, because that namespace is **writable by any agent acting
in the user's session**, and a value under policy must not be one any agent
can overwrite.

A third-party OAuth token is the strongest possible case for that rule
(§5.3). The argument transfers unchanged; do not re-litigate it.

## 3. Who holds the token — the decision everything hangs on

### 3.1 The blast radius the bridge already has

`bridge-python-code-agents.md` §3.1 refused to run operator code in the
bridge process, and the reason was custody: the bridge holds
`BP_MCP_BRIDGE_SERVICE_SECRET` (which mints invitations for *any* bridged
agent), every bridged agent's `credentials.json`, and every resolved
`env://` secret for every MCP server. One stray `open()` is full
impersonation.

Per-user tokens change the *nature* of that blast radius, not just its size.
Today a compromised bridge yields the operator's own service credentials —
bad, and bounded by what the operator provisioned. With per-user tokens it
yields **every user's third-party accounts**: their GitHub, their Drive,
their Linear. And the bridge is the process that also runs code agents, in a
container with deliberately unrestricted egress (§3.4 of that doc explains
why per-agent egress control is unachievable there).

So "just store the tokens and let the bridge read them" is the option that
looks simplest and is disqualified by an argument this repo has already
made in writing.

### 3.2 Three custody options

| | Where the long-lived token lives | Bridge sees | Verdict |
| --- | --- | --- | --- |
| **A. Bridge-held** | Bridge state dir or fetched from router at startup | The token, indefinitely | **No.** §3.1. Also means the bridge must refresh, so it needs the client secret too. |
| **B. Router-held, short-lived hand-out** | Router DB (encrypted) | A short-TTL access token, per call | **Yes for v2.** Bridge never holds refresh tokens or client secrets; a bridge compromise is bounded by the TTL. |
| **C. Router-proxied** | Router DB (encrypted) | Nothing | Architecturally best; the token is attached after the call leaves the bridge. But it makes the router an MCP client, which is a much larger change. Defer. |

**Choose B**, with C recorded as the end state. B is the same trade the
platform already makes for LLM provider keys: agents hold no provider
credentials and call `ctx.llm`, and the router attaches the real key. The
difference is only that the resource here is an MCP server rather than an
LLM.

Under B the refresh token and the OAuth client secret **never leave the
router**. The bridge asks the router for a usable access token for
`(server_id, user_id)` and gets one with minutes of life, or a typed
"not connected" answer (§9).

> C is what Anthropic's own Managed Agents vaults do — the secret is
> substituted into the outbound request at egress and never enters the
> sandbox. Worth knowing that the model has prior art, and worth not
> pretending B is equivalent: under B a bridge compromise still yields a
> window of live access tokens for whoever called during it.

### 3.3 Encryption at rest is a prerequisite, not a follow-up

**This repo encrypts nothing at rest.** Verified: the only crypto
dependency is `itsdangerous` (which *signs* — it does not encrypt), there is
no `cryptography` dependency, and `llm_presets.api_key` is stored as
plaintext `text` in Postgres.

Storing operator provider keys in plaintext is a defensible posture: the
operator chose it, the keys are theirs, and rotating one is an afternoon.
Storing **user-delegated** tokens the same way is not the same decision. A
database dump stops being a credential leak and becomes account takeover for
every user who ever connected an account — including accounts the operator
has no relationship with and cannot rotate on the user's behalf.

So the sequencing is not negotiable:

1. An encrypted secret column with a key that is **not** in the same
   database — envelope encryption against a KMS, or at minimum a key from the
   router's environment, with the key id stored alongside the ciphertext so
   rotation is possible.
2. Only then, per-user credentials.

Step 1 is independently worthwhile: it retroactively fixes
`llm_presets.api_key` and `invitations` handling has already established that
this codebase is willing to hash rather than store. It should be its own
change with its own doc, not a paragraph inside this feature.

**Do not** accept "we'll encrypt it later" here. Later means after real users
have connected real accounts, at which point the migration involves
credentials you cannot re-derive.

## 4. Per-call, not per-connection

This is where the expected cost was wrong, in the cheap direction.

### 4.1 What the client actually does

The credential is baked into a client at construction:
`server_bridge.py:327` resolves `auth_value_ref` once per bridge run and
passes it to `build_mcp_client` at `:339`; `mcp_client.py:251` turns it into
`self._headers` in `__init__`, and every request reuses that dict
(`:345`, `:391`).

The natural conclusion is that the credential is a property of the
*connection*, so per-user credentials mean one connection per
`(server, user)` — which would reshape the supervisor's entire lifecycle
model (it reconciles rows → tasks; this would make it rows × users →
connections) and would interact badly with the per-agent give-up gate in
`health.py` (§15.1 of the code-agents doc), which is keyed by agent, not by
user.

**That conclusion is wrong for the current client.** `grep` for
`Mcp-Session-Id` in `mcp_client.py` returns nothing: the bridge does not
implement MCP's session header at all. Every `tools/call` is an independent
`POST` carrying its own headers. So a per-user credential is a **header
override threaded down one call path**, not a new connection:

```
handler(ctx, payload)                    tool_agent.py:194  — has ctx.user_id
  └─ _call_tool_with_retry(...)          tool_agent.py:79   — pass override
       └─ mcp_client.call_tool(...)      mcp_client.py:315 / :550 / :1085
```

`call_tool` gains a keyword-only `auth_override`; the three implementations
(streamable_http, sse, stdio) accept it, and **stdio ignores it** — a local
subprocess's credential comes from its spawned environment, which is per-row
by construction. A `per_user` stdio server is refused at the admin API.

### 4.2 Two caveats I could not resolve from the code

Both need a test against a real provider before committing to §4.1, and both
are reasons the estimate could move back toward per-connection:

  * **`initialize` may bind authorization.** The handshake (`initialize` +
    `tools/list`) runs once per client with the row's credential. If an
    upstream ties authorization to the initialized session rather than to
    each request, per-call headers will not work for that server and it needs
    a per-user connection after all. The spec permits either.
  * **Ignoring `Mcp-Session-Id` is a latent bug, and this is where it
    bites.** It works today because the credential is constant, so no server
    has had reason to care. Per-user auth is exactly the case where a server
    would want session affinity — and if we start honoring session ids, a
    session established under user A's token must not be reused for user B.
    Sorting out session handling is arguably a prerequisite rather than a
    caveat.

### 4.3 The catalogue problem

`tools/list` needs *a* credential, but the tool catalogue is a property of
the **server** while credentials are per **user**. Whose token lists the
tools?

Resolution: a `per_user` server keeps an **operator credential for the
catalogue only** — the existing `auth_value_ref`, used for `initialize` +
`tools/list` and the `tools_cache` write — and uses per-user credentials
exclusively for `tools/call`. This keeps the supervisor's one-bridge-per-row
model intact and keeps `expose_to_llm` / `disabled_tools` / mode
reconciliation working untouched.

The cost is honest and worth writing down: **the mode set is the operator's
view, not the user's.** If a user's token can reach fewer tools than the
operator's, the agent still advertises the full set and the extra ones fail
at call time. Per-user tool lists would require per-user connections (§4.1)
and per-user `set_modes`, which the router's agent model does not express —
an agent has one mode set.

## 5. Data model

### 5.1 `mcp_servers.auth_kind = 'per_user'`

A fourth value on the existing CHECK. The current constraint pair is:

```sql
CONSTRAINT mcp_servers_auth_kind_check
  CHECK (auth_kind IN ('none', 'bearer', 'header')),
CONSTRAINT mcp_servers_auth_consistent CHECK (
  (auth_kind = 'none'   AND auth_value_ref IS NULL     AND auth_header_name IS NULL)
  OR (auth_kind = 'bearer' AND auth_value_ref IS NOT NULL AND auth_header_name IS NULL)
  OR (auth_kind = 'header' AND auth_value_ref IS NOT NULL AND auth_header_name IS NOT NULL)
)
```

`per_user` needs a branch where `auth_value_ref` is **optional** — present
when the operator supplies a catalogue credential (§4.3), absent for a
server whose `tools/list` needs no auth. Plus the OAuth registration fields
of §6.2. Note this lands as a **new migration `0002`**, the first one after
the consolidated baseline.

### 5.2 `user_mcp_credentials`

```sql
CREATE TABLE user_mcp_credentials (
    user_id        text NOT NULL
        REFERENCES users (user_id) ON UPDATE CASCADE ON DELETE CASCADE,
    server_id      text NOT NULL
        REFERENCES mcp_servers (server_id) ON UPDATE CASCADE ON DELETE CASCADE,
    -- Ciphertext + the id of the key that encrypted it, so rotation is
    -- possible without a flag day (§3.3). NEVER a plaintext token column.
    access_token   bytea NOT NULL,
    refresh_token  bytea,
    key_id         text  NOT NULL,
    expires_at     timestamptz,
    scopes         text[] NOT NULL DEFAULT '{}',
    connected_at   timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    last_used_at   timestamptz,
    PRIMARY KEY (user_id, server_id)
);
```

`ON DELETE CASCADE` on both FKs is deliberate: deleting the server or the
user must not leave an orphaned third-party token. `purge_user` must delete
these rows and **should also attempt provider-side revocation** — a purged
user's token still working against GitHub is a GDPR problem the tombstone
model does not cover (`users.purged_at`).

`last_used_at` exists so the webapp can show "last used" and so an unused
connection can be reaped; it is a write on a hot path, so update it lazily
(at most once per hour per row), not per call.

### 5.3 Why not user-scoped session state

Because that namespace is writable by any agent acting in the user's
session. `router-resolved-preset-slots.md` §5.1 refused to put a *model
choice* there on those grounds. An OAuth token is categorically worse: an
agent that could write this key could substitute its own credential and have
every other user's task call a third party as an identity of its choosing.

The general rule, worth stating once more because this is its clearest
instance: **user-scoped state holds values the router stores, never values
the router obeys.**

## 6. The OAuth flow

### 6.1 What transfers from `security/oidc.py`

Reusable more or less as-is: `generate_state`, `generate_nonce`,
`generate_pkce`, the discovery fetch + cache, and the authorization-URL
builder. The back-channel split the webapp already uses for login
(`POST /v1/auth/oidc/authorize` → `{authorize_url, state, …}`, webapp owns
the browser redirect and the transient state) is the right shape here too
and should be copied, not reinvented.

What does not transfer: `id_token` validation (an MCP server is a resource
server, not an identity provider — there may be no `id_token` at all), and
the singleton lifetime (§2.2).

### 6.2 Per-server provider registration

Each `per_user` server needs its own OAuth client registration, set by the
operator in the admin UI: `oauth_issuer` (or explicit
`authorization_endpoint` / `token_endpoint` for providers without discovery),
`oauth_client_id`, `oauth_client_secret_ref`, `oauth_scopes`.

`oauth_client_secret_ref` is a **reference** (`env://…`), not a literal —
the same posture as `auth_value_ref` and as `code_agents.secret_refs`. This
is the constraint the code-agents admin API already enforces
(`_check_secret_refs`); reuse it rather than writing a second validator.

The redirect URI must be allowlisted exactly as `oidc_allowed_redirect_uris`
already is (`_check_oidc_redirect_uri`) — an open redirect on this endpoint
hands a user's authorization code to an attacker.

### 6.3 Refresh

The router owns it (§3.2 option B). Refresh lazily on hand-out: if the
stored `expires_at` is inside a buffer, refresh, re-encrypt, store, and
return the new access token. A failed refresh marks the connection stale and
turns the next hand-out into "not connected" (§9) rather than an error — the
user's remedy is to reconnect, and the agent should say so.

Do **not** add a background refresh sweep in v1. A user who has not used a
connection in months does not need their token kept warm, and a sweep is a
scheduled job whose failure mode is silent.

## 7. Runtime path

1. Handler has `ctx.user_id` and its `server_id` (closure, as today).
2. Bridge asks the router: `GET /v1/mcp/credentials/{server_id}?user_id=…`
   — **agent-authenticated**, and the router must verify the calling agent
   *is* the bridge agent for that server, and that the `user_id` matches the
   task. A bridge that can ask for arbitrary `(server, user)` pairs is the
   whole feature's authorization hole.
3. Router returns `{access_token, expires_at}` (short TTL) or
   `{status: "not_connected"}`.
4. Handler passes the token as `auth_override` into `call_tool` (§4.1).
5. Cache the token in the bridge **in memory only**, keyed by
   `(server_id, user_id)`, until `expires_at` minus a buffer. Never to disk —
   the state dir is the thing §3.1 is about.

The existing `_call_tool_with_retry` transient-retry policy applies
unchanged, with one addition: a `401`/`403` from the upstream should
invalidate the cached token and retry **once** with a fresh hand-out, then
surface not-connected. That is the case where the user revoked access
provider-side and we have no other way to learn it.

## 8. Webapp surface

A **Connections** page under settings, following the existing page pattern
in `bp_agents/agents/webapp/pages/` (`config.py` is the closest model).
Lists the `per_user` MCP servers with, per row: connected / not connected,
scopes, last used, and Connect / Disconnect.

Connect runs the back-channel authorize (§6.1) and redirects; the callback
lands on the webapp, which posts the code to the router for exchange.
Disconnect deletes the row and attempts provider-side revocation.

Writes go under the **user's session JWT**, which is the precedent
`/v1/llm/preferences` set: there is no agent-facing write path to a user's
credentials, by construction.

Admin UI additions are ordinary: the `per_user` option in the existing
`auth_kind` select on the MCP server form, plus the §6.2 fields, plus a
read-only count of connected users (never the tokens).

## 9. The new failure mode: not connected

Today an MCP tool call either succeeds, returns an MCP `isError`, or raises.
Per-user credentials add a fourth state — *this user has not connected their
account* — and it must not be flattened into any of the other three.

It is not an ACL denial (the ACL permits the call; the credential is
missing), and surfacing it as a bare `401` tool error produces an assistant
that says "the tool failed" when the correct answer is
*"connect your GitHub account in Settings → Connections."*

So it needs a typed result the orchestrator can act on: a distinguishable
error class with the server id and a deep link, and prompt guidance to relay
it verbatim rather than retry. This is small and easy to skip, and skipping
it is most of the difference between a feature users can self-serve and a
support ticket.

## 10. Phasing

**Phase 0 — encrypted secrets at rest.** §3.3. Its own change, its own doc.
Blocks everything below.

**Phase 1 — pasted bearer token, no OAuth.** `auth_kind = 'per_user'`, the
`user_mcp_credentials` table, the router hand-out endpoint, the
`auth_override` thread, the webapp Connections page with a token field, and
the not-connected error class.

This is the phase that earns its keep: it exercises the entire per-user
path — custody, hand-out, override, catalogue split, failure mode — for a
fraction of the work, and it is the only way to settle §4.2's two caveats
against a real provider. If per-call auth turns out not to work, we learn it
here, before building an OAuth flow on top of the assumption.

**Phase 2 — OAuth authorization-code + refresh.** Generalize
`security/oidc.py` to N providers, add the refresh grant, per-server
registration (§6.2), Connect/Disconnect. Phase 1's storage and runtime path
are unchanged; only how the token is *obtained* differs.

**Phase 3 (maybe never) — router-proxied calls.** §3.2 option C.

## 11. Non-goals

  * **Per-user tool lists.** §4.3. Needs per-user connections and per-user
    mode sets; the router's agent model has one mode set per agent.
  * **`per_user` for stdio servers.** A local subprocess's credential is its
    environment, which is per-row. Refuse at the admin API.
  * **Dynamic client registration** (RFC 7591). Operators register manually.
  * **Per-user *code agent* secrets.** Same custody question, different
    feature; `code_agents.secret_refs` is operator-scoped on purpose.
  * **Sharing one connection across users.** That is what `auth_kind`
    `bearer` / `header` already are.

## 12. What not to do

  * **Don't let the bridge hold a refresh token or an OAuth client secret.**
    §3.1–3.2. It holds the invitation-minting service secret and every
    agent's credentials, and it runs operator code with unrestricted egress.
    Short-lived access tokens, in memory, or nothing.
  * **Don't store tokens before there is encryption at rest.** §3.3. "We'll
    encrypt it later" means migrating credentials you cannot re-derive, after
    real users have connected real accounts.
  * **Don't put credentials in user-scoped session state.** §5.3. Any agent
    in the session can write it. The rule is: state the router *stores*, not
    state the router *obeys*.
  * **Don't reuse an MCP session across users.** Today the bridge ignores
    `Mcp-Session-Id` entirely, so the question is dormant; the moment session
    handling is implemented, a session opened under one user's token must
    never serve another's (§4.2).
  * **Don't skip the not-connected error class.** §9. It is the difference
    between a user fixing this themselves and filing a ticket.
  * **Don't let the bridge request arbitrary `(server, user)` pairs.** §7
    step 2. The hand-out endpoint must verify the caller is that server's
    bridge agent and that the user matches the task, or the feature is an
    impersonation API.
  * **Don't write a second `env://` reference validator.** The code-agents
    admin API already has `_check_secret_refs` (§6.2). Two validators for one
    rule will diverge.
  * **Don't add a background refresh sweep.** §6.3. Refresh on use; a
    scheduled job that silently stops leaves users with dead connections and
    no signal.
