# OIDC for the webapp (Authelia / Keycloak / Google / Microsoft)

> **Status:** implemented. The router is the OIDC relying party; the
> `user_oidc_identities` table (migration 0007), `OIDC_*` settings,
> `bp_router/security/oidc.py`, and the `/v1/auth/oidc/*` endpoints provide
> discovery → code exchange → id_token validation → provisioning → first-party
> `TokenPair` issuance, and the webapp drives the browser redirects.
>
> **Implementation note — BFF split.** §2 below frames the router "as the RP"
> with browser-facing endpoints; the shipped shape keeps the router as the
> identity authority but exposes its OIDC endpoints as **back-channel JSON
> APIs** (`POST /v1/auth/oidc/authorize` → `{authorize_url, state, nonce,
> code_verifier}`; `POST /v1/auth/oidc/exchange` → `TokenPair`). The webapp
> BFF owns the browser redirects and holds the transient state/nonce/PKCE
> verifier in its signed session cookie (consistent with the existing
> password-login BFF pattern; the client secret never leaves the router).

Add Single-Sign-On to the webapp so humans can authenticate against an
external OpenID Provider (OP) — self-hosted **Authelia** or **Keycloak**,
or hosted **Google / Microsoft** — instead of (or alongside) the
email+password login. The design keeps the **router as the identity
authority**: OIDC only changes *how a human proves who they are at the
front door*; everything downstream (ACL, agents, sessions, cron, the
webapp BFF cookie, the channel-link flow) keeps consuming the router's
own session JWTs unchanged.

## 1. Why this is mostly additive

The router already mints **first-party** credentials after authentication:
`login` verifies a password, then issues a short-lived **session JWT**
(`issue_session_token`, `bp_router/security/jwt.py`) plus a **refresh
token** (`insert_refresh_token`) as a `TokenPair`. The webapp BFF stores
that pair in its signed session cookie (`store_login`,
`bp_agents/agents/webapp/auth.py`) and refreshes it proactively.

OIDC slots in *in front of* that issuance: validate an OP's `id_token`,
resolve it to a `user_id`, then call the **same** `issue_session_token` /
`insert_refresh_token` path. Consequences:

- The rest of the system is untouched — it only ever sees router JWTs.
- **Refresh works unchanged**: the refresh token is first-party, so an
  OIDC user only returns to the OP when their *refresh* token expires
  (~24 h), not on every 15-min access-token cycle.
- Any frontend (webapp, `bp_admin` BFF, future mobile) reuses one flow.

## 2. Decision — the router is the Relying Party (RP)

Put the OAuth2/OIDC Authorization-Code dance in **`bp_router`**, not the
webapp BFF.

| | Router is RP (chosen) | Webapp BFF is RP |
| --- | --- | --- |
| Token issuance | one path (`issue_session_token`) | BFF must call a router endpoint to mint a session for an OP identity |
| Reuse | webapp + admin BFF + mobile share it | each frontend re-implements |
| Trust | identity stays in the authority that already owns it | splits identity across two services |
| JWKS / discovery / IdP egress | router (already makes outbound calls) | BFF gains a new dependency |

The webapp change is then minimal: a "Sign in with SSO" button that
redirects to the router, and a return that stores the resulting
`TokenPair` exactly like password login.

```
Browser → webapp /login ("Sign in with SSO")
        → GET  /v1/auth/oidc/login      (router: redirect to OP; PKCE+state+nonce)
        → [OP authenticates the human]
        → GET  /v1/auth/oidc/callback    (router: validate id_token → user → TokenPair)
        → webapp stores access+refresh in its signed cookie   ← identical to password login
```

### 2.1 New router endpoints

- **`GET /v1/auth/oidc/login`** — read the OP discovery document
  (`/.well-known/openid-configuration`), build the authorization-code
  redirect with `scope=openid email profile [groups]`, **PKCE (S256)**,
  `state`, and `nonce`. Persist `state` / `nonce` / `pkce_verifier` in a
  short-lived (~10 min) **encrypted httpOnly cookie** (reuse the pattern
  in `docs/design/admin-session-cookie-encryption.md`) so the callback can
  verify them without server state.
- **`GET /v1/auth/oidc/callback`** — verify `state`, exchange the code at
  the token endpoint, **validate the `id_token`** (§5), resolve / provision
  the user (§4), then issue the normal `TokenPair` and hand it back to the
  frontend.

## 3. Identity model — `user_oidc_identities` (one user, many identities)

Do **not** store the OIDC subject in `users.auth_secret_hash`. That field
holds *either* a password hash *or* a single secret, and `auth_kind` is
scalar — overloading it forces password↔OIDC to be mutually exclusive per
user and caps a user at one IdP. Both are wrong:

- a **Telegram-first** account (§6) already uses `auth_secret_hash` for its
  password and must be able to *add* an SSO identity without losing it;
- a single human may link **multiple OPs** (IdP migration; corporate Azure
  AD *and* Google; work + personal).

Multiple identities per account is inherently one-to-many, so model it as a
child table — structurally the **same pattern** as the channel mapping
`suite_platform_mappings` (`(platform, external_id) → user_id`):

```sql
CREATE TABLE user_oidc_identities (
    user_id        text NOT NULL REFERENCES users(user_id) ON UPDATE CASCADE,
    issuer         text NOT NULL,          -- the OP's `iss`
    sub            text NOT NULL,          -- subject, unique only *per issuer*
    email_at_link  text,                   -- profile snapshot, NOT authority
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_login_at  timestamptz,
    UNIQUE (issuer, sub)                    -- one OP identity ↔ exactly one account
);
CREATE INDEX ON user_oidc_identities (user_id);   -- reverse lookup
```

Constraints that matter:

- **Unique on `(issuer, sub)`, never `sub` alone** — `sub` is only unique
  per issuer. The composite key also prevents two accounts claiming the
  same OP identity; a link attempt that collides → `409` + a security
  audit event.
- **Reverse index on `user_id`** — powers an account-settings "linked
  logins" pane (list / unlink) and lets us enforce "can't unlink your last
  remaining login method."
- **Login resolution**: validated `(iss, sub)` → row → `user_id` → issue
  session. No row → JIT-provision or reject per policy (§4).

### 3.1 The account becomes an identity hub

With this table, `users` is a hub with three layered, independent proof
sources — all one-to-many off the account except the single password:

| Source | Where | Cardinality |
| --- | --- | --- |
| Password | `users.auth_secret_hash` | 0–1 |
| OIDC identities | `user_oidc_identities (issuer, sub)` | 0–N |
| Chat channels | `suite_platform_mappings (platform, external_id)` | 0–N |

`auth_kind` then degrades from "the one method" to a **policy flag** —
e.g. whether direct password login is permitted for this user — while the
*provable* identities live in the mapping tables. That's a more honest
model than overloading `auth_secret_hash`, and it makes "one human, many
ways to prove it" work uniformly.

## 4. Provisioning & authorization (level assignment)

A successful OIDC login must map to a `user` row carrying a `level`
(`admin | service | tierN`). Two modes, selectable per deployment:

1. **JIT provisioning, gated by an IdP group→level claim**
   *(recommended for self-hosted SSO)*. Authelia/Keycloak already enforce
   access policy at their portal, so anyone reaching the callback is
   authorized. On first login create an account, set `email` from claims,
   and derive `level` from a configured `groups`→`level` map (fallback to a
   default tier). Double-gating with the internal pending-registration
   queue is redundant when the OP is the gate.
2. **Match-existing-only** — OIDC succeeds only for accounts that already
   exist (provisioned via the Telegram/web registration flow or by an
   admin); an unknown `sub` is rejected or routed into the existing pending
   queue for admin approval. Stricter; reuses the approval UX.

`groups` requires the `groups` scope (Authelia/Keycloak emit it). Keep the
mapping config-driven so the same code serves every OP.

## 5. OIDC protocol — security must-haves

- **Authorization Code flow + PKCE (S256)** — never implicit.
- **`state`** (CSRF on the redirect) and **`nonce`** (id_token replay),
  carried in the encrypted transient cookie and checked on callback.
- **`id_token` validation**: signature via the OP's **JWKS** (cache keys,
  honour `kid`); `iss` matches the configured issuer; `aud == client_id`;
  `exp` / `iat` / `nbf`; `nonce` matches.
- **Discovery-driven**: resolve authorize / token / jwks / `end_session`
  endpoints from `/.well-known/openid-configuration` so providers are
  config, not code.
- **Client secret** from the secrets backend (env-ref), like every other
  router secret.

## 6. Telegram-first users (interop with the current path)

### 6.1 What exists today (password bootstrap)

A Telegram user reaches the webapp entirely via password:

1. `/register [email]` → admin approves → `user` row, `auth_kind="password"`,
   random initial password, `serviced_by=[chatbot service principal]`.
2. **`/password`** in the bot (`gateway.py` `_cmd_password`) →
   `mint_password_reset_token` (F9, `serviced_by`-gated) → a one-time token.
3. Webapp **`/set-password`** consumes it and sets the web password.
4. `/login` with email + that password.

The chat identity *vouches* (via `serviced_by`) to mint a token; the user
converts it into a web password. The token is the trust bridge.

### 6.2 The collision

OIDC resolves by `(iss, sub)`, but a Telegram-first user is a `password`
account with **no linked sub**. Naively, OIDC login would JIT a **second,
separate account** (different `user_id`, no shared sessions / memory / cron
/ `serviced_by`). And they can't "log in then link," because they can't
log into the webapp yet — the same chicken-and-egg `/password` solved.

### 6.3 Handling (any of, per deployment)

1. **Token-link bootstrap (universal, recommended).** Reuse the `/password`
   token but redeem it on the **OIDC callback** to *attach* the sub instead
   of setting a password — the OIDC analogue of `/set-password`. Flow: bot
   `/password` → user starts "Connect SSO" carrying that token (preserved
   through `state`) → callback validates the `id_token`, then **consumes the
   token → resolves its `user_id` → inserts `(iss, sub)`** for that account.
   Works even when the account has no email on file. (This is the direct
   sibling of the channel-link `link-channel` endpoint: a single-use token
   proves account ownership so a new identity can be attached.)
2. **Auto-link by verified email (low-friction, single trust domain).**
   Behind a config flag (`oidc_auto_link_by_verified_email`, **off by
   default**): on first SSO login, if no `sub` matches but a user has the
   same `email_verified` address, link automatically. Convenient when the
   operator controls both Authelia and the Telegram registration emails;
   an account-takeover vector otherwise, so never use it for unverified or
   absent emails.
3. **OIDC-first, Telegram-as-channel (cleanest going forward).** For *new*
   users, flip the relationship: sign into the webapp via SSO
   (JIT-provisioned), then link Telegram with the existing channel
   link-token flow (`/link`). No new mechanism — the mirror image of today
   (Telegram-identity + linked web password → OIDC-identity + linked
   Telegram channel). Only *pre-existing* Telegram-first users need options
   1 or 2.

### 6.4 What stays unaffected after linking

It's **one account**: the chatbot service principal stays in `serviced_by`
(cron notifications to Telegram keep working); sessions / memory / cron are
shared; and the link-token mint is `auth_kind`-agnostic, so an OIDC user
can still link *more* channels. `/password` recovery becomes N/A for an
OIDC-only user (recovery = re-auth at the OP), and the webapp should hide
set/change-password UI for `auth_kind="oidc"` users.

## 7. Logout

Existing logout (clear the BFF cookie + revoke the router refresh token)
ends the **router** session. Optionally add **RP-initiated logout** — a
redirect to the OP's `end_session_endpoint` — so an SSO logout propagates
to the OP session too. The two are independent; document the distinction.

## 8. Settings (new, `OIDC_`-prefixed)

`enabled`, `issuer` (discovery base), `client_id`, `client_secret` (secret
ref), `redirect_uri`, `scopes`, `jit_provisioning` (bool), `default_level`,
`group_to_level` (map), optional `allowed_groups` allowlist,
`auto_link_by_verified_email` (bool, default false).

## 9. Provider notes

Drive everything off the discovery document so **Authelia, Keycloak,
Google, Microsoft** (all OIDC-compliant) work unchanged; `groups` is the
level-mapping source where supported. **GitHub is OAuth2, not true OIDC**
(no `id_token`) — it would need a small userinfo adapter; note it but it's
out of scope for the self-hosted (Authelia/Keycloak) and Google/MS cases
that motivate this.

## 10. Phasing

1. **Router RP** — discovery + `oidc/login` + `oidc/callback`, JWKS
   validation, the `user_oidc_identities` table (**migration**), JIT
   provisioning, `TokenPair` issuance, settings.
2. **Webapp** — "Sign in with SSO" on `/login`; return handler stores the
   `TokenPair` via `store_login`; hide password UI for `auth_kind="oidc"`.
3. **Linking & polish** — group→level mapping, the token-link bootstrap
   (§6.3.1), an account-settings "linked logins" pane (list / unlink),
   RP-initiated logout.

## 11. Non-goals / open items

- **GitHub / non-OIDC OAuth2** providers (need a userinfo adapter).
- **Per-user password *and* SSO simultaneously enabled** is supported by
  the schema, but the exact `auth_kind` policy semantics (does linking SSO
  disable password login?) are an operator decision to nail down in Phase 3.
- **SCIM / deprovisioning** — reacting to an OP disabling a user is out of
  scope here (the user's `level` / `suspended_at` remains the router's
  control surface).
- **TOTP** stays orthogonal — it's a password-login second factor
  (`docs/backplaned/security.md`); OIDC defers MFA to the OP.
