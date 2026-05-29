# Admin webUI — Operations & Security Posture

The admin UI (`bp_admin`) is a thin BFF that talks only to the
router's `/v1/admin/*` and `/v1/auth/*` JSON endpoints. It is mounted
under `/admin` by the router by default (toggle with
`ROUTER_SERVE_ADMIN_UI`).

This doc covers the parts that aren't in the page-level handler
docstrings: deployment, security posture, and the threat model the UI
is built against.

## 1. Deployment

The UI is shipped in the same Python package and runs in the same
process as the router. There is no separate service to deploy. To turn
it off entirely, set `ROUTER_SERVE_ADMIN_UI=false` on the router.

A future "split-process" mode can run `bp-admin` as its own service
talking to a remote router via `ADMIN_ROUTER_URL`. The middleware,
session management, and templates are identical; only the deployment
shape differs.

### 1.1 Required environment

| Var | Required | Default | Notes |
| --- | --- | --- | --- |
| `ADMIN_SESSION_SECRET` | **yes** | — | Signs the session cookie. Generate with `openssl rand -hex 32`. Must be unique per deployment. |
| `ADMIN_ROUTER_URL` | no | `http://127.0.0.1:8000` | Loopback in same-process mode; set when split. |
| `ADMIN_DEPLOYMENT_ENV` | no | `dev` | One of `dev`, `staging`, `prod`. Logged on startup. |

### 1.2 Production checklist

These defaults are tuned for development. **Flip them before
exposing the UI to a non-loopback network:**

| Setting | Dev default | Production target |
| --- | --- | --- |
| `ADMIN_SESSION_COOKIE_SECURE` | `true` (secure-by-default) | `true` (cookie sent over HTTPS only; set `false` only for local http://localhost dev) |
| `ADMIN_SESSION_COOKIE_SAME_SITE` | `strict` | `strict` (keep) |
| `ADMIN_SESSION_COOKIE_MAX_AGE_S` | `86400` (1 day) | tune to your operational window |
| HTTPS termination | none | required — login posts the password as form data |
| Reverse proxy | optional | recommended; cap request body sizes, rate-limit `/admin/login` |

If you serve the UI on plain HTTP, the login password and the session
cookie travel in the clear. Don't.

## 2. Authentication & sessions

- Login posts email + password to `/admin/login`. The handler forwards
  to upstream `/v1/auth/login` and stashes the JWT pair plus a fresh
  CSRF token in the session cookie. Non-admin accounts are rejected
  with the same generic "Invalid credentials." message.
- Subsequent requests carry the access token; the BFF middleware
  proactively refreshes when the token has fewer than
  `ADMIN_REFRESH_BUFFER_S` seconds remaining (default 30s).
- Session cookie is signed by Starlette's `SessionMiddleware` using
  `ADMIN_SESSION_SECRET`. Tampering invalidates the cookie. Cookie
  storage is server-side opaque: payload is base64+HMAC, not
  encrypted, so don't put true secrets in the session.
- The login page itself is exempt from auth and CSRF.

## 3. CSRF protection

State-changing requests (`POST` / `PUT` / `PATCH` / `DELETE`) carry a
double-submit token bound to the session cookie:

- A 32-byte URL-safe random token is minted on login and stored in
  `request.session["csrf_token"]`.
- Form submissions echo it back via a hidden `csrf_token` field
  (rendered by `bp_admin/templates/_partials/csrf.html`).
- HTMX and `fetch` submissions echo it back via the `X-CSRF-Token`
  header. The base template's `<meta name="csrf-token">` plus a
  global `htmx:configRequest` listener wires this automatically; the
  one direct-`fetch` site (ACL drag-drop reorder) reads the same meta.
- The CSRF middleware (`bp_admin.csrf.make_csrf_middleware`) checks
  the echoed value against the session copy in constant time. Mismatch
  → `403`. Login (`/admin/login`) and `/static/*` are exempt.

The token rotates on every login. There is no per-request rotation —
the token's lifetime equals the session's, and the cookie is bound to
the user agent that authenticated.

## 4. Threat model

The admin UI's threat model inherits from
[`docs/security.md`](./security.md). Notable points specific to the
UI:

- **CSRF** — covered by the double-submit token (§3) plus
  `SameSite=Strict`. Subdomain takeover scenarios still warrant the
  token; SameSite alone is not sufficient.
- **XSS** — Jinja2 autoescapes HTML output for `.html` templates
  (default). User-supplied data (agent_id, user_id, ACL patterns,
  audit payloads, test-task output) flows through autoescape. Inline
  JavaScript context is avoided: row navigation is delegated via
  `data-href` rather than interpolated `onclick="..."` strings.
- **Session theft** — the session cookie is the bearer credential.
  Run only over HTTPS in production. Set `ADMIN_SESSION_COOKIE_SECURE`.
- **Open redirect** — the `next` param after login is restricted to
  paths under `/admin/` via `_safe_next` in `auth_pages.py`.
- **Credential disclosure on failed login** — the same "Invalid
  credentials." message is returned for unknown user, wrong password,
  or non-admin role; status code differs (401 vs 403) but body
  doesn't.
- **Information disclosure via flash** — flash messages live in URL
  query strings and survive in browser history. Don't render
  PII-bearing strings into them.
- **Insider with admin role** — out of scope. The audit log captures
  every admin action; review it.

## 5. Operational notes

- `bp_admin/static/admin.css` is a thin layer over Tailwind (loaded
  via Play CDN). For production, consider compiling Tailwind locally
  and serving the result instead — the CDN is a third-party
  availability dependency.
- HTMX, Alpine, and SortableJS likewise ship from public CDNs. The
  `<script>` tags in `base.html` and `acl/list.html` carry no
  Subresource Integrity hashes today; pin them or self-host before any
  serious public deployment.
- Logging out POSTs to `/admin/logout` which forwards a logout to the
  router (best-effort) and then clears the session cookie.

## 6. LLM presets

The **LLM presets** sidebar entry (`/admin/llm/presets`) wraps the
`/v1/admin/llm/presets` API. Each preset bundles the configuration
agents reference at call time:

- **Provider** — one of `gemini`, `anthropic`, `openai`,
  `openai-embeddings`, `openai-compatible`,
  `openai-compatible-embeddings`. Pinned dropdown to prevent typos
  that'd 400 the API. The two `openai-compatible*` values target
  local servers (vLLM, LM Studio, llama.cpp, Ollama OpenAI-mode,
  etc.) and reveal an additional **Endpoint base URL** field.
- **Concrete model** — the upstream's model id
  (`gemini-2.5-flash`, `claude-opus-4-8`, `gpt-5.5`,
  `text-embedding-3-small`, `qwen2.5-32b`, etc.). No validation —
  operators may experiment with new model snapshots before they're
  added to defaults.
- **Endpoint base URL** — required for `openai-compatible*` (no
  default endpoint to fall back to), optional for hosted providers
  (blank = upstream SDK default). On hosted providers it overrides
  the official endpoint, enabling Azure OpenAI proxies, AWS Bedrock
  fronts for Anthropic, regional Vertex / EU Gemini, LiteLLM /
  Portkey gateways, etc. Examples:
  `http://localhost:8000/v1` (vLLM),
  `http://localhost:1234/v1` (LM Studio),
  `http://localhost:11434/v1` (Ollama OpenAI-mode),
  `https://<resource>.openai.azure.com/openai/deployments/<deploy>`
  (Azure OpenAI). The router includes `base_url` in the adapter
  cache key, so two presets that differ only in endpoint stay
  isolated and hit the right upstream. URLs are validated against
  an SSRF blocklist on save: hosted providers must use `https://`
  and may not point at private / loopback / link-local addresses or
  cloud-metadata hostnames. Operators with private-VPC gateways at
  known hostnames carve exceptions via the env var
  `ROUTER_BASE_URL_ALLOWED_HOSTS` (comma-separated). See
  `docs/sdk/services.md` §1.1.2 for the full policy.
- **API key reference** — a `secret_ref` like
  `env://OPENAI_API_KEY`. The router resolves it via
  `bp_router.security.secrets.resolve_secret_ref` at first use and
  caches the resolved adapter. Optional for `openai-compatible*`
  presets — local servers usually don't authenticate, in which case
  the adapter sends the placeholder `"EMPTY"`.
- **Inline API key** — optional plaintext secret stored directly on
  the preset row. When set, it wins over the reference; otherwise
  the reference is resolved as usual. The form input is a password
  field; the API never echoes the value back. The list page shows
  a green **inline key** badge on rows that carry one, and the edit
  form surfaces a "Clear the existing inline key" checkbox when one
  is present. Pasting a new value replaces any existing inline key.
- **Min user level** — `*` / `admin` / `service` / `tierN`. Same
  grammar as ACL rules. Defaults to `*` (anyone) for built-in
  presets.
- **Default temperature** / **max tokens** — overridable at call
  time.
- **Default `provider_options`** — JSON object holding per-provider
  knobs (Anthropic `thinking`, OpenAI `reasoning`, Gemini
  `thinking_level`, etc.). Call-time `provider_options` REPLACES
  this entirely (not merged) — agents override only when they want
  a wholly different config.
- **Fallback preset** + **Max retries** — wire up retry + fallback
  behaviour for non-streaming calls. The router tries this preset
  `max_retries+1` times before walking to `fallback_preset`; the
  walk follows the chain transitively. Streaming calls (`stream=True`)
  skip retry / fallback entirely. Cycles are rejected on save with
  a 400 (the chain message names every node involved). Mid-chain
  fallback targets that fail the user's tier gate are silently
  skipped — see `docs/sdk/services.md` §1.1 for the exact semantics.

Mutations trigger an in-memory reload of the preset map on the
router so the next LLM call sees the new shape immediately. User
level changes (via `/admin/users/{id}/level`) drop the affected
user's cached level so a demotion takes effect on the next call,
not minutes later.

**Audit trail.** Every CRUD action emits an audit event:
`llm_preset.created`, `llm_preset.updated`, `llm_preset.deleted`,
visible from the audit log page filtered by
`event=llm_preset.*`. Inline `api_key` values are masked to `"***"`
in audit payloads, and clearing an inline key shows up as
`api_key_cleared: true`.

**Operational tip.** Cost-sensitive deployments: gate
`gpt-5.5-pro`, `claude-opus-4-8`, and high-effort reasoning presets
to `tier0` or `service` (exclude `tier3+`). Cheap presets stay at
`*`. The full grammar from `docs/acl.md` §3.4 applies — `tier0` is
the most privileged non-admin level, `tier3+` is the least.
