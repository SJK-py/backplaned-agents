# Environment-variable reference

<!-- GENERATED FILE — do not edit.
     Regenerate with: python scripts/gen_env_reference.py
     Source of truth: the settings models listed below. -->

Every setting each component reads, with its default. This file is generated
from the Pydantic settings models, so it cannot drift from the code.

For the handful a deployment actually sets, start with
[`.env.example`](../.env.example) (router / SDK / suite quick-start) and
[`deploy/.env.prod.example`](../deploy/.env.prod.example) (production).

Defaults shown as `(required)` have no default — the process will not start
without them. Defaults shown as `(computed)` are derived at load time.


## Router (`bp_router`)

Environment prefix: `ROUTER_`

| variable | default | notes |
| --- | --- | --- |
| `ROUTER_ACCESS_LOG_QUIET_PATHS` | `['/healthz', '/metrics', '/v1/admin/serviced-sessions', '/v1/admin/mcp-servers', '/v1/admin/metrics']` |  |
| `ROUTER_ACL_MAX_TIER` | `3` |  |
| `ROUTER_ADMIN_SESSION_SECRET` | `None` |  |
| `ROUTER_ADMIN_TEST_ALLOW_ACT_AS` | `False` |  |
| `ROUTER_AGENT_INFO_UPDATE_RATE_LIMIT_PER_AGENT_BURST` | `5` |  |
| `ROUTER_AGENT_INFO_UPDATE_RATE_LIMIT_PER_AGENT_PER_S` | `1.0` |  |
| `ROUTER_AGENT_TOKEN_TTL_S` | `86400` |  |
| `ROUTER_BASE_URL_ALLOWED_HOSTS` | `''` |  |
| `ROUTER_BIND_HOST` | `'0.0.0.0'` |  |
| `ROUTER_BIND_PORT` | `8000` |  |
| `ROUTER_BOOTSTRAP_ADMIN_EMAIL` | `None` |  |
| `ROUTER_BOOTSTRAP_ADMIN_PASSWORD` | `None` |  |
| `ROUTER_CALLER_AGENT_CACHE_MAX` | `10000` |  |
| `ROUTER_CHANGE_PASSWORD_RATE_LIMIT_PER_USER_BURST` | `3` |  |
| `ROUTER_CHANGE_PASSWORD_RATE_LIMIT_PER_USER_PER_S` | `0.05` |  |
| `ROUTER_CLOSED_SESSION_RETENTION_DAYS` | `90` |  |
| `ROUTER_DB_POOL_MAX_SIZE` | `10` |  |
| `ROUTER_DB_POOL_MIN_SIZE` | `1` |  |
| `ROUTER_DB_STATEMENT_TIMEOUT_MS` | `30000` |  |
| `ROUTER_DB_URL` | (required) |  |
| `ROUTER_DEFAULT_TASK_DEADLINE_S` | `900` |  |
| `ROUTER_DEPLOYMENT_ENV` | `'dev'` |  |
| `ROUTER_FILE_DEFAULT_TTL_S` | `604800` |  |
| `ROUTER_FILE_DOWNLOAD_PRESIGNED` | `False` |  |
| `ROUTER_FILE_FETCH_TOKEN_TTL_S` | `3600` |  |
| `ROUTER_FILE_STORAGE_QUOTA_BYTES` | `{'admin': None, 'service': None, 'tier0': None, 'tier1': 1073741824, 'tier2': 268435456, 'tier3': 67108864}` |  |
| `ROUTER_FILE_STORE` | `'local'` |  |
| `ROUTER_FILE_STORE_OPTIONS` | `{}` |  |
| `ROUTER_FILE_UPLOAD_REQUEST_RATE_LIMIT_PER_AGENT_BURST` | `20` |  |
| `ROUTER_FILE_UPLOAD_REQUEST_RATE_LIMIT_PER_AGENT_PER_S` | `5.0` |  |
| `ROUTER_FILE_UPLOAD_TOKEN_TTL_S` | `300` |  |
| `ROUTER_HEARTBEAT_INTERVAL_MS` | `20000` |  |
| `ROUTER_JWT_ALGORITHM` | `'HS256'` |  |
| `ROUTER_JWT_KEY_VERSION` | `1` |  |
| `ROUTER_JWT_SECRET` | (required) |  |
| `ROUTER_LINK_TOKEN_MINT_RATE_LIMIT_PER_USER_BURST` | `5` |  |
| `ROUTER_LINK_TOKEN_MINT_RATE_LIMIT_PER_USER_PER_S` | `0.0028` |  |
| `ROUTER_LINK_TOKEN_TTL_S` | `900` |  |
| `ROUTER_LLM_ATTACHMENT_INLINE_MAX_BYTES` | `5242880` |  |
| `ROUTER_LLM_DEFAULT_PRESETS` | `{'pro': 'default', 'balanced': 'default', 'lite': 'default'}` |  |
| `ROUTER_LLM_IMAGE_MAX_LONG_SIDE_PX` | `1568` |  |
| `ROUTER_LLM_IMAGE_RESCALE_SOURCE_MAX_BYTES` | `20971520` |  |
| `ROUTER_LLM_PRESET_CATALOG_PATH` | `None` |  |
| `ROUTER_LLM_PRESET_OVERLAY_PATH` | `None` |  |
| `ROUTER_LLM_REQUEST_MAX_FILE_REFS` | `16` |  |
| `ROUTER_LOG_LEVEL` | `'INFO'` |  |
| `ROUTER_LOGIN_RATE_LIMIT_PER_EMAIL_BURST` | `5` |  |
| `ROUTER_LOGIN_RATE_LIMIT_PER_EMAIL_PER_S` | `0.1` |  |
| `ROUTER_LOGIN_RATE_LIMIT_PER_IP_BURST` | `5` |  |
| `ROUTER_LOGIN_RATE_LIMIT_PER_IP_PER_S` | `0.2` |  |
| `ROUTER_MAX_PAYLOAD_BYTES` | `1048576` |  |
| `ROUTER_MAX_REQUEST_BODY_BYTES` | `65536` |  |
| `ROUTER_MAX_UPLOAD_BYTES` | `52428800` |  |
| `ROUTER_MCP_ALLOWED_LAUNCHERS` | `['uvx']` |  |
| `ROUTER_MCP_BRIDGE_SECRET` | `None` |  |
| `ROUTER_METRICS_TOKEN` | `None` |  |
| `ROUTER_OIDC_ALLOWED_GROUPS` | `[]` |  |
| `ROUTER_OIDC_ALLOWED_REDIRECT_URIS` | `[]` |  |
| `ROUTER_OIDC_AUTO_LINK_BY_VERIFIED_EMAIL` | `False` |  |
| `ROUTER_OIDC_CLIENT_ID` | `None` |  |
| `ROUTER_OIDC_CLIENT_SECRET` | `None` |  |
| `ROUTER_OIDC_DEFAULT_LEVEL` | `'tier1'` |  |
| `ROUTER_OIDC_DISCOVERY_CACHE_TTL_S` | `3600` |  |
| `ROUTER_OIDC_ENABLED` | `False` |  |
| `ROUTER_OIDC_GROUP_CLAIM` | `'groups'` |  |
| `ROUTER_OIDC_GROUP_TO_LEVEL` | `{}` |  |
| `ROUTER_OIDC_HTTP_TIMEOUT_S` | `10.0` |  |
| `ROUTER_OIDC_ISSUER` | `None` |  |
| `ROUTER_OIDC_JIT_PROVISIONING` | `True` |  |
| `ROUTER_OIDC_SCOPES` | `'openid email profile'` |  |
| `ROUTER_OTEL_ENDPOINT` | `None` |  |
| `ROUTER_OTEL_SERVICE_NAME` | `'bp_router'` |  |
| `ROUTER_PASSWORD_RESET_CONSUME_RATE_LIMIT_PER_IP_BURST` | `20` |  |
| `ROUTER_PASSWORD_RESET_CONSUME_RATE_LIMIT_PER_IP_PER_S` | `2.0` |  |
| `ROUTER_PASSWORD_RESET_MINT_RATE_LIMIT_PER_TARGET_BURST` | `3` |  |
| `ROUTER_PASSWORD_RESET_MINT_RATE_LIMIT_PER_TARGET_PER_S` | `0.000833` |  |
| `ROUTER_PASSWORD_RESET_TOKEN_TTL_S` | `600` |  |
| `ROUTER_PENDING_ACK_TIMEOUT_S` | `30.0` |  |
| `ROUTER_PER_SOCKET_OUTBOX_MAX` | `256` |  |
| `ROUTER_PUBLIC_URL` | (required) |  |
| `ROUTER_QUOTA_ADMIT_BURST` | `{'admin': None, 'service': None, 'tier0': 200, 'tier1': 40, 'tier2': 10, 'tier3': 2}` |  |
| `ROUTER_QUOTA_ADMIT_RATE_PER_S` | `{'admin': None, 'service': None, 'tier0': 100.0, 'tier1': 20.0, 'tier2': 5.0, 'tier3': 1.0}` |  |
| `ROUTER_REFRESH_RATE_LIMIT_PER_IP_BURST` | `20` |  |
| `ROUTER_REFRESH_RATE_LIMIT_PER_IP_PER_S` | `2.0` |  |
| `ROUTER_REFRESH_TOKEN_TTL_S` | `86400` |  |
| `ROUTER_REGISTRATION_RATE_LIMIT_PER_EXTERNAL_BURST` | `5` |  |
| `ROUTER_REGISTRATION_RATE_LIMIT_PER_EXTERNAL_PER_S` | `0.0014` |  |
| `ROUTER_REGISTRATION_RATE_LIMIT_PER_SUBMITTER_BURST` | `60` |  |
| `ROUTER_REGISTRATION_RATE_LIMIT_PER_SUBMITTER_PER_S` | `1.0` |  |
| `ROUTER_REGISTRATION_WEB_RATE_LIMIT_PER_IP_BURST` | `5` |  |
| `ROUTER_REGISTRATION_WEB_RATE_LIMIT_PER_IP_PER_S` | `0.0014` |  |
| `ROUTER_RESUME_WINDOW_S` | `30` |  |
| `ROUTER_SERVE_ADMIN_UI` | `True` |  |
| `ROUTER_SERVICE_MINT_REFRESH_TOKEN_RATE_LIMIT_PER_TARGET_BURST` | `5` |  |
| `ROUTER_SERVICE_MINT_REFRESH_TOKEN_RATE_LIMIT_PER_TARGET_PER_S` | `0.00333` |  |
| `ROUTER_SESSION_JWT_TTL_S` | `900` |  |
| `ROUTER_SESSION_STORE_QUOTA_BYTES` | `{'admin': None, 'service': None, 'tier0': None, 'tier1': 268435456, 'tier2': 67108864, 'tier3': 16777216}` |  |
| `ROUTER_SHUTDOWN_GRACE_S` | `25.0` |  |
| `ROUTER_SPAWN_MAX_DEPTH` | `16` |  |
| `ROUTER_TASK_DELEGATION_MAX_DEPTH` | `32` |  |
| `ROUTER_VALKEY_URL` | `None` |  |
| `ROUTER_WS_HANDSHAKE_CATALOG_CACHE_TTL_S` | `5.0` |  |
| `ROUTER_WS_HANDSHAKE_MAX_CONCURRENT` | `8` |  |
| `ROUTER_WS_HANDSHAKE_RATE_LIMIT_PER_IP_BURST` | `20` |  |
| `ROUTER_WS_HANDSHAKE_RATE_LIMIT_PER_IP_PER_S` | `5.0` |  |


## Agent SDK (`bp_sdk`)

Environment prefix: `AGENT_`

| variable | default | notes |
| --- | --- | --- |
| `AGENT_AUTH_TOKEN` | `None` |  |
| `AGENT_EMBEDDED` | `False` |  |
| `AGENT_INVITATION_TOKEN` | `None` |  |
| `AGENT_LOG_LEVEL` | `'INFO'` |  |
| `AGENT_ONBOARD_URL` | `None` |  |
| `AGENT_PENDING_ACKS_TIMEOUT_S` | `30.0` |  |
| `AGENT_PENDING_BUFFER_MAX_SIZE` | `1024` |  |
| `AGENT_PENDING_BUFFER_WINDOW_S` | `5.0` |  |
| `AGENT_PENDING_RESULTS_TIMEOUT_S` | `480.0` |  |
| `AGENT_PROGRESS_BUFFER_SIZE` | `256` |  |
| `AGENT_RECONNECT_INITIAL_BACKOFF_S` | `0.5` |  |
| `AGENT_RECONNECT_MAX_BACKOFF_S` | `30.0` |  |
| `AGENT_RECV_CONSECUTIVE_FAILURES_MAX` | `16` |  |
| `AGENT_REONBOARD_MAX_ATTEMPTS` | `3` |  |
| `AGENT_ROUTER_URL` | `'ws://localhost:8000/v1/agent'` |  |
| `AGENT_SERVICE_REFRESH_TOKEN` | `None` |  |
| `AGENT_SERVICE_TOKEN_EXPIRES_AT` | `None` |  |
| `AGENT_SERVICE_USER_ID` | `None` |  |
| `AGENT_STATE_DIR` | `PosixPath('agent_state')` |  |
| `AGENT_WS_MAX_RECEIVE_BYTES` | `2097152` |  |


## Agent suite (`bp_agents`)

Environment prefix: `SUITE_`

| variable | default | notes |
| --- | --- | --- |
| `SUITE_BRAVE_API_KEY` | `None` |  |
| `SUITE_DATABASE_URL` | `'postgresql://postgres:bp@127.0.0.1:5432/bp_suite'` |  |
| `SUITE_DATALAB_API_KEY` | `None` |  |
| `SUITE_DB_POOL_MAX_SIZE` | `10` |  |
| `SUITE_DB_POOL_MIN_SIZE` | `1` |  |
| `SUITE_DB_STATEMENT_TIMEOUT_MS` | `30000` |  |
| `SUITE_DEFAULT_LANGUAGE` | `'en'` |  |
| `SUITE_DEFAULT_MAX_CONTEXT_TOKEN_LIMIT` | `120000` |  |
| `SUITE_DEFAULT_PRESET_BALANCED` | `'default'` |  |
| `SUITE_DEFAULT_PRESET_EMBEDDING` | `'default_embedding'` |  |
| `SUITE_DEFAULT_PRESET_LITE` | `'default'` |  |
| `SUITE_DEFAULT_PRESET_MULTIMODAL` | `''` |  |
| `SUITE_DEFAULT_PRESET_PRO` | `'default'` |  |
| `SUITE_DEFAULT_TIMEZONE` | `'UTC'` |  |
| `SUITE_DELEGATABLE_AGENTS` | `['research', 'computer_use', 'deep_reasoning']` |  |
| `SUITE_DISPATCH_RESULT_TIMEOUT_S` | `600.0` |  |
| `SUITE_EMBEDDING_DIM` | `1536` |  |
| `SUITE_EXA_API_KEY` | `None` |  |
| `SUITE_EXA_SEARCH_TYPE` | `'auto'` |  |
| `SUITE_KAGI_API_KEY` | `None` |  |
| `SUITE_KAKAO_CALLBACK_DEADLINE_S` | `50.0` |  |
| `SUITE_KAKAO_CALLBACK_TTL_S` | `60.0` |  |
| `SUITE_KAKAO_CARRY_TTL_S` | `900` |  |
| `SUITE_KAKAO_CF_ACCOUNT_ID` | `None` |  |
| `SUITE_KAKAO_CF_API_TOKEN` | `None` |  |
| `SUITE_KAKAO_CF_QUEUE_ID` | `None` |  |
| `SUITE_KAKAO_MSG_CHAR_LIMIT` | `1000` |  |
| `SUITE_KAKAO_PULL_BATCH_SIZE` | `10` |  |
| `SUITE_KAKAO_PULL_VISIBILITY_TIMEOUT_S` | `60` |  |
| `SUITE_KAKAO_R2_ACCESS_KEY_ID` | `None` |  |
| `SUITE_KAKAO_R2_BUCKET` | `None` |  |
| `SUITE_KAKAO_R2_DOWNLOAD_URL_TTL_S` | `3600` |  |
| `SUITE_KAKAO_R2_ENDPOINT_URL` | `None` |  |
| `SUITE_KAKAO_R2_SECRET_ACCESS_KEY` | `None` |  |
| `SUITE_KAKAO_R2_URL_TTL_S` | `600` |  |
| `SUITE_KB_EMBED_BATCH_SIZE` | `100` |  |
| `SUITE_KB_MAX_CHUNK_LEN` | `2000` |  |
| `SUITE_KB_META_HEAD_CHARS` | `8000` |  |
| `SUITE_KB_META_TAIL_CHARS` | `2000` |  |
| `SUITE_KB_MIN_CHUNK_LEN` | `1000` |  |
| `SUITE_KB_OVERLAP_LEN` | `100` |  |
| `SUITE_LANCE_ROOT` | `'./suite_lance'` |  |
| `SUITE_MD_BACKEND` | `'markitdown'` |  |
| `SUITE_MD_CONVERT_MEM_LIMIT_MB` | `0` |  |
| `SUITE_MD_CONVERT_TIMEOUT_S` | `120.0` |  |
| `SUITE_MD_DATALAB_BASE_URL` | `'https://www.datalab.to'` |  |
| `SUITE_MD_DATALAB_MODE` | `'balanced'` |  |
| `SUITE_MD_DATALAB_TIMEOUT_S` | `300.0` |  |
| `SUITE_MD_OCR_API_KEY` | `None` |  |
| `SUITE_MD_OCR_BASE_URL` | `None` |  |
| `SUITE_MD_OCR_MAX_RETRIES` | `1` |  |
| `SUITE_MD_OCR_MODEL` | `None` |  |
| `SUITE_MD_OCR_PROMPT` | `None` |  |
| `SUITE_MD_OCR_TIMEOUT_S` | `60.0` |  |
| `SUITE_MEMORY_DECAY_FLOOR` | `0.5` |  |
| `SUITE_MEMORY_DECAY_START_DAYS` | `30` |  |
| `SUITE_MEMORY_GC_HORIZON_DAYS` | `100` |  |
| `SUITE_MEMORY_GC_INTERVAL_S` | `86400.0` |  |
| `SUITE_MEMORY_PURGE_ALLOWED_PRINCIPAL` | `None` |  |
| `SUITE_MEMORY_RECONCILE_CANDIDATES` | `5` |  |
| `SUITE_MEMORY_RETRIEVE_POOL` | `50` |  |
| `SUITE_PLAN_MAX_ITERS` | `24` |  |
| `SUITE_PLAN_MAX_STEPS` | `12` |  |
| `SUITE_PLAN_STEP_TIMEOUT_S` | `240.0` |  |
| `SUITE_SANDBOX_BASH_TIMEOUT_S` | `120.0` |  |
| `SUITE_SANDBOX_MAX_INLINE_OUTPUT` | `8000` |  |
| `SUITE_SANDBOX_RLIMIT_AS_BYTES` | `2147483648` |  |
| `SUITE_SANDBOX_RLIMIT_CPU_S` | `120` |  |
| `SUITE_SANDBOX_RLIMIT_FSIZE_BYTES` | `1073741824` |  |
| `SUITE_SANDBOX_RLIMIT_NPROC` | `256` |  |
| `SUITE_SANDBOX_ROOT` | `'/home'` |  |
| `SUITE_SANDBOX_UID_BASE` | `2000` |  |
| `SUITE_SANDBOX_UID_MAX` | `60000` |  |
| `SUITE_SEARXNG_URL` | `None` |  |
| `SUITE_SELECTABLE_PRESETS_BALANCED` | `[]` |  |
| `SUITE_SELECTABLE_PRESETS_LITE` | `[]` |  |
| `SUITE_SELECTABLE_PRESETS_PRO` | `[]` |  |
| `SUITE_SESSION_GC_INTERVAL_S` | `86400.0` |  |
| `SUITE_SESSION_GC_RETENTION_DAYS` | `90` |  |
| `SUITE_TELEGRAM_BASE_URL` | `'https://api.telegram.org'` |  |
| `SUITE_TELEGRAM_BOT_TOKEN` | `None` |  |
| `SUITE_TELEGRAM_POLL_TIMEOUT_S` | `25` |  |
| `SUITE_TEXT_ONLY_PRESETS` | `[]` |  |
| `SUITE_VALKEY_URL` | `None` |  |
| `SUITE_VERBOSE_DETAIL_CHARS` | `100` |  |
| `SUITE_WEB_DEEP_CHUNK_CHARS` | `2000` |  |
| `SUITE_WEB_DEEP_FETCH_MULTIPLIER` | `2` |  |
| `SUITE_WEB_DEEP_MIN_SNIPPET_CHARS` | `80` |  |
| `SUITE_WEB_DEEP_THIN_FRACTION` | `0.5` |  |
| `SUITE_WEB_DEEP_TOP_CHUNKS` | `3` |  |
| `SUITE_WEB_EXTRACT_FETCH_CHARS` | `16000` |  |
| `SUITE_WEB_FETCH_MAX_BYTES` | `52428800` |  |
| `SUITE_WEB_FETCH_MAX_REDIRECTS` | `3` |  |
| `SUITE_WEB_FETCH_TIMEOUT_S` | `120.0` |  |
| `SUITE_WEB_FETCH_USER_AGENT` | `'Mozilla/5.0 (compatible; BackplanedBot/1.0)'` |  |
| `SUITE_WEB_SEARCH_BACKEND` | `'searxng'` |  |
| `SUITE_WEB_SEARCH_DEEP` | `'auto'` |  |

