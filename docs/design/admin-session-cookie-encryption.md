# Admin session cookie encryption (M1)

> **Status:** deferred future work. No code changes proposed in
> this document — captured here so the gap doesn't disappear into
> the review backlog. A focused PR will pick this up once we
> commit to a session-storage backend.
>
> **Scope:** the second-pass review's M1 finding —
> `bp_admin/auth.py:54-67` writes the upstream `access_token`
> AND `refresh_token` into Starlette's `SessionMiddleware`
> cookie, which is signed but **not encrypted**. The session
> cookie is base64-decodable to plaintext JWTs by anyone who
> obtains the cookie value (XSS, malicious browser extension,
> shared-machine snoop, leaked HAR file).
>
> **Outcome we want:** the session cookie carries an opaque
> session-id only; the actual tokens live in a server-side store
> (Valkey preferred — already a dependency for JTI revocation).
> Cookie-stealer attacks degrade from "instant token theft" to
> "instant session takeover until logout" — still bad, but the
> tokens themselves don't leak and a server-side revocation can
> kill them immediately.

---

## 1. The gap today

`bp_admin/auth.py:49-67` does:

```python
def store_login(request, *, login_response, email):
    request.session["access_token"] = login_response["access_token"]
    request.session["access_expires_at"] = login_response["expires_at"]
    request.session["refresh_token"] = login_response["refresh_token"]
    request.session["level"] = login_response["level"]
    request.session["email"] = email
    request.session["csrf_token"] = _issue_csrf_token()
    ...
```

`request.session` is Starlette's `SessionMiddleware`, which stores
its dict as JSON inside an HMAC-signed cookie (`itsdangerous`-style).
Signing prevents tampering but does NOT prevent reading. The full
cookie value, base64-decoded, reveals the JWTs verbatim.

This is documented Starlette behaviour — not a Starlette bug. The
guidance is "don't put secrets in `SessionMiddleware`." We did.

The cookie is `HttpOnly` (`SessionMiddleware` default) and as of
PR #74 also `Secure`-by-default (M8), so the obvious leak vectors
— JS reading via `document.cookie`, MitM on plain HTTP — are
closed. The remaining attack surface:

  - **Stored XSS** that reads via a cookie-stealing payload that
    bypasses HttpOnly (e.g. service-worker registration on a
    misconfigured origin). HttpOnly is necessary, not sufficient.
  - **Browser-extension snoop**. Extensions with `cookies`
    permission see all cookies regardless of HttpOnly.
  - **Shared-machine residue**. The cookie persists on disk in
    the user's browser profile; another user with FS access to
    the profile dir reads it cleanly.
  - **Leaked HAR / debug captures**. Operator pastes a HAR into a
    bug report; the JWTs are right there, valid for hours.

For all four, the difference between "cookie holds tokens" and
"cookie holds opaque session-id" is the difference between
"attacker has tokens until natural expiry" and "attacker has
session until next admin clicks logout / operator deletes the
Valkey key."

## 2. Two natural designs

### 2.1 Server-side session store (recommended)

Cookie holds an opaque session-id (`secrets.token_urlsafe(32)`);
all token state lives in Valkey under `admin_session:{session_id}`.
Format:

```
HSET admin_session:{sid}
    access_token  <jwt>
    access_expires_at  <iso8601>
    refresh_token  <jwt>
    user_id  <uuid>
    level  admin
    email  <addr>
    csrf_token  <token>
EXPIRE admin_session:{sid}  <session_max_age_s>
```

  - Cookie value: ~43 chars (b64-encoded 32 bytes), nothing
    sensitive. Signed for tamper-resistance is still useful but no
    longer load-bearing for confidentiality.
  - Logout = `DEL admin_session:{sid}` — instant, server-side,
    irrespective of the browser still holding the cookie.
  - Admin-side "kick all sessions" = `SCAN MATCH admin_session:*`
    + filter by user_id. Useful operational lever.
  - Refresh-token rotation: hot-swap the stored values on a
    successful refresh; cookie unchanged.

Valkey is already a soft dependency (JTI revocation set, future
quota work in `quota-enforcement.md`). Hardening this dependency
to "required" is a one-line `Settings` validator change.

### 2.2 Encrypted cookie (fallback)

Switch from `SessionMiddleware` to a Fernet-wrapped session cookie.
Cookie payload becomes ciphertext + AEAD tag; only the BFF (which
holds the key) can decrypt.

  - Pro: no infrastructure dependency. Stateless BFF survives
    Valkey outages.
  - Pro: minimal migration — replace one middleware, keep the
    `request.session[...]` API.
  - Con: cookie size grows (Fernet adds ~57 bytes overhead +
    base64 expansion). Browsers cap at ~4 KiB total per domain;
    headroom is fine but the JWT pair already eats ~1.5 KiB.
  - Con: revocation still requires JTI-set Valkey (already true
    for the upstream tokens). No server-side "kick this session"
    capability — only natural expiry.
  - Con: key rotation is a real operation. The signing key for
    `SessionMiddleware` already exists (`session_secret`); adding
    a separate encryption key means two secrets to rotate.

Pick this only if a deployment commits to single-binary,
no-Valkey operation. Otherwise §2.1 is strictly better.

## 3. Recommended approach: §2.1 with Valkey

Three migrations:

  1. **Add `RedisSessionStore` class.** Lives in
     `bp_admin/session_store.py`. Methods: `create(payload)`,
     `read(sid)`, `update(sid, patch)`, `delete(sid)`,
     `extend(sid, ttl_s)`. Wraps `redis.asyncio.Valkey` with
     `HSET`/`HGETALL`/`DEL`/`EXPIRE` calls.

  2. **Replace SessionMiddleware with a thin shim.** New
     `OpaqueSessionMiddleware` reads the session-id from a signed
     cookie, fetches the dict from Valkey on request entry,
     mounts it on `request.state.session`, writes back any
     mutations on response. The existing call sites
     (`request.session["access_token"]`, etc.) get a one-line
     adapter so the rest of `bp_admin` doesn't notice.

  3. **Logout DELs the key.** Currently `clear_session` calls
     `request.session.clear()` — which empties the in-memory
     dict but leaves the cookie holding stale tokens until the
     middleware writes back an empty dict. With the Valkey store,
     `clear_session` becomes `await store.delete(sid)` and the
     cookie is unset on the response. No more "logged-out
     session still has valid tokens" race.

## 4. Settings shape

```python
class AdminConfig(BaseSettings):
    # Existing.
    session_secret: SecretStr               # cookie signing only
    session_cookie_max_age_s: int = 86_400  # cookie + Valkey TTL
    session_cookie_secure: bool = True

    # NEW.
    session_store: Literal["cookie", "redis"] = "cookie"
    """`cookie` keeps the legacy SessionMiddleware behaviour for
    deployments that haven't migrated yet. `redis` switches to
    the opaque-session-id model."""

    session_redis_url: Optional[str] = None
    """Required when `session_store == "redis"`. Falls back to the
    main router's REDIS_URL if both processes share Valkey (the
    common deployment shape)."""
```

Per-deployment opt-in via env var. Migration is a no-op for
deployments that don't flip the switch.

## 5. Implementation plan

Two small PRs:

  1. **`RedisSessionStore` + `OpaqueSessionMiddleware`.** New
     module, no call-site changes. Tests against `fakeredis`
     prove the round-trip + TTL behaviour.

  2. **Wire it in.** `bp_admin/main.py` swaps middleware based on
     `config.session_store`. Tests exercise the migration path:
     legacy cookie still readable while in `cookie` mode; flip
     to `redis` and the new sessions land in Valkey. No
     downtime-migration tooling — admins re-login on the cutover
     (acceptable; the admin user count is small).

## 6. Why this isn't being done now

Three reasons:

  1. **No deployment is hitting it.** The current operator runs
     a single-tenant deployment behind SSO; the admin user count
     is one. Cookie-stealer XSS isn't a credible vector because
     there's no cross-user content rendered in the admin UI.

  2. **Valkey-or-stateless ambiguity.** The right backend choice
     depends on whether the deployment has Valkey available
     (multi-worker setup) or is single-binary (no Valkey). The
     review-fix bundle PRs left that decision unresolved; same
     unresolved-Valkey question gates this work as the quota one
     (`quota-enforcement.md` §4).

  3. **Adjacent CSRF refactor pending.** The CSRF token is
     currently stored in the same session dict
     (`bp_admin/csrf.py`). When we move tokens to Valkey we want
     the CSRF token to ride along — splitting it out into a
     separate cookie would create a second migration. Bundling
     is cheaper but means waiting until both items are ready.

## 7. Tracking

This doc is the durable home for the M1 finding. When a
deployment commits to Valkey-backed sessions, start here and walk
the implementation plan in §5.
