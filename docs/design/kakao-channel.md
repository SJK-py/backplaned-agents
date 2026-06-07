# KakaoTalk channel (relay + queue, pull-only)

Add KakaoTalk as a second chatbot channel alongside Telegram. A
KakaoTalk "i Open Builder" skill cannot be polled and will not let the
agent push at will: it delivers each user turn as a **webhook the bot
must answer within ~5 s**, and any later message must ride a
**one-shot `callbackUrl` that expires after ~1 minute**. To keep the
agent **egress-only** (no public inbound, no router exposure) we put a
tiny stateless **Cloudflare Worker relay** in front, hand each turn to a
**Cloudflare Queue**, and have the agent **pull** jobs over plain HTTPS —
mirroring the Telegram `_poll_loop` ([channel.md §4](../agent-suite/channel.md)).
The shared `ChannelCore` (per-session lock + dispatch + result relay,
`bp_agents/channel/core.py`) is reused unchanged; only the **transport
and the delivery model** are new.

## 1. Why Kakao is not Telegram

Telegram gives the channel a **symmetric** transport: the bot long-polls
`getUpdates` (`bp_agents/agents/chatbot/telegram.py`) and can
`sendMessage` to a `chat_id` **at any time**. That is what lets
`ChatbotGateway` await a turn for up to `dispatch_result_timeout_s`
(default 600 s) and then push the answer whenever it lands, plus stream
verbose progress as separate messages.

Kakao's skill model breaks every one of those assumptions:

| | Telegram | KakaoTalk skill |
| --- | --- | --- |
| Inbound | bot pulls (`getUpdates`) | Kakao **pushes** a webhook; bot has no listener of its own |
| Sync budget | none | the webhook **must** return a JSON body in **~5 s** |
| Async reply | `sendMessage` anytime | one **`callbackUrl`**, single use, **~1 min TTL** |
| Unprompted push | yes | **no** (only paid AlimTalk templates) |
| Images out | upload bytes | needs a **public `imageUrl`** Kakao's servers fetch |
| Identity | `chat_id` (`update.message.chat.id`) | `userRequest.user.id` (per-bot hashed id) |

Three consequences drive the whole design:

  1. **The agent must not be the webhook server.** Answering Kakao in 5 s
     from inside the suite would force a public inbound port on the agent
     (or the router) and couple Kakao's SLA to dispatch latency. We keep
     the agent egress-only and absorb the 5 s ack at the edge.
  2. **A turn usually outlives the callback.** A real dispatch (tools,
     delegation, LLM) routinely exceeds 1 minute, but the `callbackUrl`
     does not. The channel needs an explicit **deadline state machine**
     (§7) — answer within the window when it can, otherwise promise to
     finish and deliver on the user's **next touch**.
  3. **Outbound images need a public URL.** The router's blob store is
     internal-only and downloads stream **through** the router
     (`ROUTER_FILE_DOWNLOAD_PRESIGNED=false`, [deployment.md §6](../backplaned/deployment.md)),
     so there is no Kakao-reachable image URL to hand out. We host
     outbound images on a dedicated bucket (§8).

## 2. Goals / non-goals

**Goals**

  * A KakaoTalk channel that reaches feature parity with the Telegram
    text path: registration, per-session serialization, dispatch,
    commands, and an answer delivered back to the user.
  * **Agent stays egress-only.** No inbound port on the agent or router;
    no high-value secret on the public surface.
  * **Reuse `ChannelCore`** verbatim — the per-session lock, turn
    injection, and result relay are transport-agnostic already.
  * A principled **deadline + next-touch** delivery model that survives
    the 1-minute callback TTL and agent restarts.
  * Inbound and outbound **images**.
  * **Zero behavior when unconfigured** — exactly like
    `telegram_bot_token` being unset, the consumer task simply never
    launches.

**Non-goals**

  * Unprompted push / proactive notifications. Deferred to an optional
    AlimTalk follow-up (§14, PR5); the baseline delivers on next touch.
  * Verbose per-frame progress streaming. Kakao has one callback per
    turn, not a message stream; verbose collapses to a single status +
    final answer (§7).
  * A second identity system. Kakao reuses the existing
    `suite_platform_mappings` registration flow (§9).
  * Cron/scheduled delivery to Kakao. Same push limitation; out of scope
    until AlimTalk lands.

## 3. Architecture

```
KakaoTalk  ──webhook(5s)──▶  Cloudflare Worker (relay)        PUBLIC, stateless
                               1. verify X-Kakao-Skill-Secret
                               2. reply {useCallback:true, data:{text:"처리 중…"}}
                               3. KAKAO_JOBS.send({chat_id, utterance,
                                                   image_url, callback_url, received_at})
                                          │ producer binding
                                          ▼
                               Cloudflare Queue  (KAKAO_JOBS)
                                          ▲ HTTP pull + ack (outbound httpx)
   AGENT (egress-only) ───────────────────┘  kakao_consume_loop → KakaoGateway.handle_job
        │  reuses ChannelCore (lock + dispatch + result relay)
        ├─ POST answer ──────▶ job.callback_url            (outbound, within TTL)
        ├─ park late turns ──▶ Redis registry              (deliver on next touch, §7)
        ├─ inbound image ────▶ router named store          (outbound /v1/files)
        └─ outbound image ───▶ R2 presigned URL            (outbound)  ──▶ Kakao fetches
```

Three components, each with a single job:

  * **Relay Worker** (`deploy/kakao-relay/`, TypeScript) — the only
    public surface. Authenticates Kakao, returns the 5 s "working…" ack,
    enqueues the turn. Holds no business logic, no DB, no router reach,
    and only one low-value secret.
  * **Cloudflare Queue** — durable buffer decoupling Kakao's 5 s SLA from
    the agent's dispatch latency, and a restart buffer (a turn that
    arrives while the agent is down waits in the queue).
  * **Agent consumer** (`bp_agents/agents/chatbot/kakao_*.py`) — an
    outbound pull loop registered exactly like `_poll_task`, feeding
    `KakaoGateway → ChannelCore`.

**Why relay + queue + pull** (vs. the agent terminating the webhook):
the agent never opens a port; Kakao's auth secret lives only at the edge;
a queue gives at-least-once durability and back-pressure for free; and
the pull loop is structurally identical to the Telegram poll loop we
already operate, so the lifecycle, shutdown, and back-off code is the
same shape. Alternatives are weighed in §15.

## 4. The relay Worker

A wrangler project under `deploy/kakao-relay/` (`wrangler.toml` +
`src/worker.ts`), deployed independently of the suite. The **entire**
relay:

```ts
export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    if (req.method !== "POST") return new Response("ok");          // health
    const got = req.headers.get("X-Kakao-Skill-Secret") ?? "";
    if (!timingSafeEqualStr(got, env.KAKAO_SKILL_SECRET))          // constant-time
      return new Response("forbidden", { status: 403 });

    const p = await req.json();
    await env.KAKAO_JOBS.send({                                    // producer binding
      chat_id:      p?.userRequest?.user?.id,
      utterance:    p?.userRequest?.utterance ?? "",
      image_url:    extractImage(p),                               // image-only ingress (§8)
      callback_url: p?.userRequest?.callbackUrl,                   // ⚠ verify field (§16)
      received_at:  Date.now(),
    });

    return Response.json({
      version: "2.0",
      useCallback: true,
      data: { text: "처리 중이에요…" },
    });
  },
};
```

  * **Stateless and dumb by design.** It never inspects the utterance,
    knows no commands, and reaches nothing private. The single secret it
    holds — `KAKAO_SKILL_SECRET`, set via `wrangler secret put` — only
    authenticates Kakao→relay; leaking it lets someone enqueue junk
    turns, not read data or reach the router.
  * **`useCallback: true` on every turn** so the relay never needs to
    distinguish a command from a question — the agent always delivers the
    real answer over the callback. This requires **callback enabled** on
    the skill in the Kakao console and the `callbackUrl` field present in
    the request (§16).
  * **Producer-only queue binding.** The Worker can `send`; it cannot
    pull or ack. The consume credential lives only on the agent.

## 5. Queue + pull consumer

`bp_agents/agents/chatbot/kakao_consumer.py` mirrors `_poll_loop`
(`bp_agents/agents/chatbot/agent.py`): a `while not stop.is_set()` loop,
network errors back off rather than tight-loop, registered as a
background task and cancelled on shutdown.

```python
logger = logging.getLogger(__name__)

async def kakao_consume_loop(
    gateway: "KakaoGateway", client: "KakaoClient", settings, stop
) -> None:
    """Pull jobs from the Cloudflare Queue and fan each out to a turn task.

    Outbound-only: POST .../messages/pull, process, POST .../messages/ack.
    At-least-once — dedupe on the queue message id (§13). A pull/network
    error backs off rather than tight-looping, matching _poll_loop.
    """
    while not stop.is_set():
        try:
            msgs = await client.pull(
                batch_size=settings.kakao_pull_batch_size,
                visibility_timeout_s=settings.kakao_pull_visibility_timeout_s,
            )
        except Exception:  # noqa: BLE001
            logger.exception("kakao_pull_error", extra={"event": "kakao_pull_error"})
            await _sleep_or_stop(stop, 2.0)
            continue

        acks: list[str] = []
        for m in msgs:
            try:
                await gateway.handle_job(m.body)
                acks.append(m.lease_id)                 # ack only on success
            except Exception:  # noqa: BLE001
                logger.exception("kakao_job_error", extra={"event": "kakao_job_error"})
                # no ack → bounded redelivery
        if acks:
            await client.ack(acks)
```

  * **`pull` / `ack` are plain outbound `httpx`** to the Cloudflare Queues
    HTTP pull API (`POST /accounts/{acct}/queues/{id}/messages/pull` and
    `.../ack`), bearer-authed with a scoped token. The exact endpoint
    shape is verified against the current CF Queues API (§16).
  * **Ack on success only.** A failed turn is left unacked so the queue
    redelivers it (bounded by the queue's max-retries → DLQ). This is the
    same "don't lose the turn" stance as the Telegram offset only
    advancing past a handled update.
  * **Concurrency.** v1 processes a pulled batch sequentially; per-session
    ordering is then guaranteed even before `ChannelCore`'s lock. If
    throughput needs it, fan out to per-message tasks like `_inflight`
    and rely on the session lock for ordering — noted, not built.

## 6. The gateway

`bp_agents/agents/chatbot/kakao_gateway.py` is a thin transport adapter
over the shared engine — it does **not** re-implement dispatch. It
resolves identity, hands the turn to `ChannelCore`, and delivers the
result over the callback instead of `sendMessage`.

```python
class KakaoGateway:
    def __init__(self, *, dispatcher, pool, client: "KakaoClient",
                 registry: "KakaoTaskRegistry", credentials=None,
                 redis=None, settings=...) -> None:
        self._client = client
        self._registry = registry
        self._core = ChannelCore(            # reused verbatim — same as Telegram
            dispatcher=dispatcher, pool=pool, redis=redis,
            result_timeout_s=settings.dispatch_result_timeout_s,
            delegatable_agents=frozenset(settings.delegatable_agents),
            fire_memory=True,
        )

    async def handle_job(self, job: dict) -> None:
        if await self._registry.seen(job["msg_id"]):   # at-least-once dedupe (§13)
            return
        chat_id, callback_url = job["chat_id"], job["callback_url"]

        # identity — same resolution as Telegram, platform="kakao"
        user_id, session_id = await self._resolve(chat_id)
        if user_id is None:
            await self._client.post_callback(callback_url, REGISTER_PROMPT)
            return

        text = job["utterance"].strip()
        if text.startswith("/"):
            return await self._handle_command(chat_id, callback_url, text)

        await self._run_turn(chat_id, callback_url, user_id, session_id, job)
```

  * **`ChannelCore` is shared, not forked.** Per-session lock, the single
    `append_user` write ([channel.md §8](../agent-suite/channel.md)),
    delegation, and the post-turn summarization check all come for free.
  * **Commands reuse the Telegram set** where they make sense (`/register`,
    `/new`, `/stop`, `/password`, `/config`, `/cron`, `/delegate`,
    `/undelegate`, `/help`) — the handlers already live in the gateway
    layer; only the **reply sink** changes from `sendMessage` to
    `post_callback`. Verbose `/v` is accepted but, lacking a message
    stream, only flips the single status line (§2 non-goal).
  * **Delivery is the hard part** and lives in §7.

`KakaoClient` (`bp_agents/agents/chatbot/kakao_client.py`) is the
transport: `pull` / `ack` (§5), `post_callback(url, text|payload)`, the
Kakao response builders (`simpleText`, `simpleImage`, `quickReplies` for
the `[Check]`/`[Stop]` buttons), and `fetch_inbound_image` (§8). No
server — symmetric with `HttpTelegramClient` but outbound-only.

## 7. The deadline state machine

This is the crux: a `callbackUrl` is **single-use and ~1 min**, but a
dispatch can take many minutes. The channel races the dispatch against a
callback deadline and degrades gracefully.

```
handle_job(job)
   │
   ├─ start dispatch via ChannelCore  (await, up to dispatch_result_timeout_s)
   │
   ├─ race against kakao_callback_deadline_s  (default 50s, < TTL)
   │
   ├─ dispatch wins  ──▶ post_callback(answer)                     [DONE]
   │
   └─ deadline wins (turn still running)
          ├─ post_callback("아직 작업 중이에요…", quickReplies=[확인, 중지])   ← spends the callback
          ├─ registry.park(chat_id, task_handle, started_at)        ← Redis, TTL = kakao_carry_ttl_s
          └─ dispatch keeps running in the background
                 └─ on completion → registry.store_result(chat_id, answer)

next inbound from same chat_id  (a new webhook → new callbackUrl)
   ├─ "[확인]" / "[중지]" quick-reply, or any message
   ├─ registry.take_result(chat_id)?
   │      ├─ ready  ──▶ post_callback(parked answer) [+ handle the new turn]
   │      └─ pending ──▶ post_callback("아직 작업 중이에요…", [확인, 중지])
   └─ "[중지]"  ──▶ recursively cancel the parked dispatch (ChannelCore cancel), confirm
```

  * **One callback, spent deliberately.** If the turn beats
    `kakao_callback_deadline_s` (≈50 s, comfortably inside the ~60 s TTL),
    the answer goes straight back. Otherwise that callback is spent on a
    *"still working — [확인] [중지]"* status, and the answer is **parked**
    in Redis for the next touch. Either button or any later message
    arrives as a fresh webhook with a **fresh** callback, which we use to
    deliver the parked result.
  * **The registry** (`bp_agents/agents/chatbot/kakao_registry.py`) is a
    small Redis-backed store, reusing the already-open `_redis`
    (`bp_agents/db/connection.py::open_redis`): keyed by `chat_id`, it
    holds the in-flight task handle (for `[중지]` cancellation), the
    parked result, and a dedupe set of seen `msg_id`s. TTL
    `kakao_carry_ttl_s` bounds how long a result waits before the user
    must just re-ask.
  * **`[중지]` cancels recursively.** It cancels the parked
    `ChannelCore` dispatch — which already tears down a delegated
    sub-task tree ([delegation.md](../agent-suite/delegation.md)) — so a
    stuck delegate chain stops too, not just the top frame.
  * **Stale-on-arrival.** If a job is pulled older than the callback TTL
    (the agent was down > 1 min, the relay's callback is already dead),
    the turn is still dispatched so the **user turn is recorded and
    answered on next touch** — losing the answer is worse than
    double-recording. Freshness is checked against `job["received_at"]`.
  * **Restart safety.** A parked result lives in Redis, not process
    memory, so an agent restart mid-turn loses the running dispatch but
    not the user's recorded turn; the user re-touches and is told to
    re-ask once the TTL lapses. (Resuming a dispatch across restart is out
    of scope.)

## 8. Files

**Inbound — reuse the router named store.** Kakao delivers an image as a
temporary public URL in the webhook; the relay forwards it as
`job.image_url`. The agent **fetches the bytes itself** (no router
outbound-fetch — that SSRF surface was deliberately removed,
[router-managed-file-store.md §4.1](./router-managed-file-store.md)) and
**stores it via the existing session-authed named-store endpoints**
(`POST /v1/files/names`, the same path Telegram attachments take,
[data-model.md §3](../agent-suite/data-model.md)). From there it is an
ordinary attachment the model can read — **no new storage code** for
inbound.

**Outbound — a dedicated public bucket (R2).** To render an image in
KakaoTalk you must give Kakao a `simpleImage.imageUrl` that **Kakao's
servers fetch directly**. The router's store can't serve that: the blob
backend is internal-only and downloads stream through the router under a
self-authorizing fetch token (`ROUTER_FILE_DOWNLOAD_PRESIGNED=false`,
[deployment.md §6](../backplaned/deployment.md)). Handing that URL to Kakao would
(a) only work in public-domain deploys, breaking LAN/bare-IP topologies,
and (b) leak a router fetch token to Kakao's CDN cache. Instead the agent
uploads the outbound image to an **R2 bucket** and hands Kakao a
**short-TTL presigned GET** — a public surface fully decoupled from the
router.

  * `bp_agents/agents/chatbot/kakao_files.py` — `R2FileEgress.put(bytes,
    content_type) -> presigned_url`, built on **`aioboto3`** (the
    **`storage-s3` extra already declares `aioboto3>=14.0`** — no new
    dependency, only new config).
  * Scope is intentionally narrow: **outbound images only**. Everything
    else (inbound, internal files) stays on the router store.

## 9. Identity & registration

Identity reuses the channel-agnostic flow unchanged
([channel.md §2](../agent-suite/channel.md)): resolution is
`chat_id → user_id → user_config.default_session_id`, keyed by
`suite_platform_mappings(platform, chat_id)`. Kakao adds one platform
value:

  * **`platform = "kakao"`**, `chat_id = userRequest.user.id` (the
    per-bot hashed user id — stable per user per bot, the right mapping
    key).
  * An **unmapped** chat gets the `/register` prompt; `/register` submits
    `POST /v1/registrations` as the **existing** chatbot service
    principal (`usr_service_chatbot`); admin approval populates the
    mapping and seeds the initial session. **No router change, no new
    endpoint** — this dissolves the earlier "L1 vs L2 linking" question:
    the platform-mapping table *is* the linking mechanism and it is
    already channel-agnostic.

**One Kakao-specific UX wrinkle:** Telegram can *push* an "approved!"
message once the admin approves (it owns the `chat_id` and can send
anytime). Kakao cannot — there is no open callback at approval time. The
user simply learns they're approved on their **next message** (the
mapping now resolves), or, once AlimTalk lands (PR5), via a push
template. Noted as expected behavior, not a gap to engineer around in v1.

## 10. Settings

New fields on `SuiteSettings` (`bp_agents/settings.py`, `env_prefix=
"SUITE_"`), all optional. **The activation gate is the queue + R2
credentials being present** (the skill secret lives on the Worker, not
here) — exactly the `telegram_bot_token is None → skip` pattern:

```python
# --- chatbot channel (KakaoTalk) ---
kakao_cf_account_id: str | None = None
kakao_cf_queue_id: str | None = None
kakao_cf_api_token: SecretStr | None = None          # scoped: Queues pull + ack
kakao_pull_batch_size: int = Field(default=10, ge=1)
kakao_pull_visibility_timeout_s: int = Field(default=60, ge=1)
kakao_callback_deadline_s: float = Field(default=50.0, gt=0)   # < ~60s TTL
kakao_carry_ttl_s: int = Field(default=900, ge=1)              # parked-result lifetime
kakao_msg_char_limit: int = Field(default=1000, ge=1)          # Kakao simpleText cap
# R2 (outbound images) — reuses aioboto3 from the storage-s3 extra
kakao_r2_endpoint_url: str | None = None
kakao_r2_bucket: str | None = None
kakao_r2_access_key_id: str | None = None
kakao_r2_secret_access_key: SecretStr | None = None
kakao_r2_url_ttl_s: int = Field(default=600, ge=1)
```

```python
def _kakao_configured(s: SuiteSettings) -> bool:
    return all((s.kakao_cf_account_id, s.kakao_cf_queue_id, s.kakao_cf_api_token))
```

When unset the agent constructs nothing Kakao-related and the consumer
task never launches — boot is byte-for-byte identical to today.

**Worker-side env** (not suite settings; set via wrangler):
`KAKAO_SKILL_SECRET` and the `KAKAO_JOBS` producer binding.

Lifecycle wiring in `bp_agents/agents/chatbot/agent.py` adds one
module-global task next to `_poll_task`:

```python
if _kakao_configured(_settings) and _redis is not None:
    _kakao = HttpKakaoClient(_settings)
    registry = KakaoTaskRegistry(_redis, ttl_s=_settings.kakao_carry_ttl_s)
    kgw = KakaoGateway(dispatcher=agent, pool=_pool, client=_kakao,
                       registry=registry, credentials=_credentials,
                       redis=_redis, settings=_settings)
    _kakao_task = asyncio.create_task(kakao_consume_loop(kgw, _kakao, _settings, _stop))
```

`_shutdown` cancels `_kakao_task` and `await _kakao.aclose()`, alongside
the existing cancellations. **Redis is required** for Kakao (the registry
backs the deadline state machine), so the gate also checks `_redis is not
None`; without it the channel logs `kakao_redis_required` and skips.

## 11. Schema & migration

The user-turn / session / file model is unchanged — Kakao writes through
`ChannelCore` like any channel. Only two **CHECK constraints** widen to
admit the new channel, so migration `0002` (the first post-baseline
migration; `down_revision = "0001_suite_initial"`,
`bp_agents/migrations/versions/`) does exactly that:

  * `session_info.channel` — add `'chatbot_kakao'` to the existing
    `IN ('chatbot_telegram','webapp')` check
    ([data-model.md §1.1](../agent-suite/data-model.md)).
  * `suite_platform_mappings.platform` — add `'kakao'` to the existing
    `IN ('telegram','web')` check
    ([data-model.md §1.6](../agent-suite/data-model.md)).

```python
# 0002_kakao_channel.py
def upgrade() -> None:
    op.execute("ALTER TABLE session_info DROP CONSTRAINT session_info_channel_check")
    op.execute("ALTER TABLE session_info ADD CONSTRAINT session_info_channel_check "
               "CHECK (channel IN ('chatbot_telegram','webapp','chatbot_kakao'))")
    op.execute("ALTER TABLE suite_platform_mappings DROP CONSTRAINT suite_platform_mappings_platform_check")
    op.execute("ALTER TABLE suite_platform_mappings ADD CONSTRAINT suite_platform_mappings_platform_check "
               "CHECK (platform IN ('telegram','web','kakao'))")
```

(Exact constraint names confirmed against the `0001` baseline before
writing the migration.)

## 12. Security — the secret split

The relay design exists to keep the **public surface valueless**:

| Surface | Reachability | Secrets held |
| --- | --- | --- |
| **Relay Worker** | public (Kakao → it) | `KAKAO_SKILL_SECRET` (gate only), queue **producer** binding |
| **Cloudflare Queue** | CF-internal | — |
| **Agent** | **egress-only**, no inbound | CF Queues **pull/ack** token, router service token, R2 keys |

  * No high-value secret, no DB credential, and no router reachability sit
    on the internet-facing component. Compromising the Worker lets an
    attacker enqueue spam turns (rate-limited, and still gated by
    per-user `suite_platform_mappings` before any dispatch) — not read
    history or reach the control plane.
  * The agent opens **zero** inbound ports; every call it makes is
    outbound (`pull`, `ack`, `post_callback`, `/v1/files`, R2). This
    preserves the suite's "agents never join the public net" posture
    ([deployment.md §7](../backplaned/deployment.md)).
  * `KAKAO_SKILL_SECRET` is compared in **constant time** at the edge.

## 13. Failure modes & idempotency

  * **At-least-once delivery.** Cloudflare Queues may redeliver. Every job
    carries a `msg_id`; `handle_job` checks `registry.seen(msg_id)` and
    drops duplicates **before** dispatch, so a redelivery can't double-run
    a turn or double-write the user row.
  * **Pull/ack failure** → unacked messages reappear after the visibility
    timeout; the loop backs off 2 s on a pull error, matching
    `_poll_loop`. Repeated poison turns hit the queue's max-retries → DLQ
    rather than spinning.
  * **Callback POST failure** (Kakao 5xx / TTL already lapsed) → log
    `kakao_callback_failed`; the answer is parked so the next touch still
    delivers it. A callback is never retried blindly (it may be expired).
  * **Dispatch error** → the turn is **acked** (the user turn was
    recorded; redelivering won't help) and an apology is sent over the
    callback, same as the Telegram error path.
  * **Agent down across a turn** → the job waits durably in the queue; on
    restart it's pulled, freshness-checked (§7), and answered on next
    touch if its original callback is dead.

## 14. Delivery plan

Phased so every PR is independently shippable and inert until configured.

  * **PR1 — plumbing (zero behavior).** `SuiteSettings` fields + gate;
    migration `0002`; the `deploy/kakao-relay/` Worker; `KakaoClient`
    `pull`/`ack` + a `kakao_consume_loop` skeleton that pulls, acks, and
    logs (no turn processing); lifecycle wiring behind the gate. Suite
    boot is unchanged when `SUITE_KAKAO_*` is unset.
  * **PR2 — text turns.** `KakaoGateway.handle_job` over `ChannelCore`,
    the deadline state machine + registry (§7), `[확인]`/`[중지]` quick
    replies, recursive `[중지]` cancel. Text-only; commands reuse the
    Telegram handlers.
  * **PR3 — images.** Inbound via the router named store; outbound via
    `R2FileEgress` presigned URLs (§8).
  * **PR4 — registration polish.** `/register` over Kakao, the
    approval-on-next-touch UX (§9), `/password` link.
  * **PR5 — (optional) AlimTalk push.** Proactive delivery of a parked
    result / approval notice via a paid template, lifting the next-touch
    constraint. Only if the product wants it.

## 15. Alternatives considered

  * **Agent terminates the webhook directly** (a Starlette/uvicorn server
    in the agent). Rejected: forces a public inbound port on the agent or
    router, puts Kakao's auth secret on the high-value box, couples the
    5 s SLA to dispatch latency, and breaks the egress-only posture. The
    relay buys all of that back for ~30 lines of Worker.
  * **Relay → agent directly (HTTP push, no queue).** Rejected: still
    needs the agent reachable from the Worker (inbound), and loses the
    durability/back-pressure a queue gives for free (a turn arriving
    while the agent restarts would be dropped). The queue + pull keeps the
    agent symmetric with the Telegram poll loop.
  * **Reuse the router file store for outbound images.** Rejected for the
    deploy-coupling and token-leak reasons in §8 — it only works in
    public-domain deploys and exposes a router fetch token to Kakao's CDN.
    R2 is topology-independent and isolates the public image surface.
  * **A separate Kakao identity/linking table.** Unnecessary —
    `suite_platform_mappings` is already channel-keyed (§9).

## 16. Open questions / external verification

Kakao/Cloudflare doc checks. Several are now **confirmed** against a live
deploy + current docs:

  * **Response shapes — confirmed.** The relay's sync **ack** uses
    `{version, useCallback: true, data: {text}}` and **must not** include a
    `template`; the later **callback POST** and any **immediate** reply use
    `{version, template: {outputs[…], quickReplies}}` (Kakao's AI-chatbot
    callback guide). The agent's `post_callback` and the relay's no-callback
    fallback follow this.
  * **CF Queues pull body — confirmed.** The HTTP pull API returns a
    json-content body as a JSON *string* (occasionally base64), not a parsed
    object; `kakao_client._coerce_body` normalizes it.

Still worth eyeballing on your skill:

  1. The exact **`callbackUrl` field path** (`userRequest.callbackUrl`) and
     that **callback mode is enabled** on the skill; confirm the callback
     **TTL** (~1 min) and **5 s** sync budget — `kakao_callback_ttl_s` /
     `kakao_callback_deadline_s` are tuned from these.
  2. The **inbound image field** shape (where `extractImage` reads the URL).
  3. That **`quickReplies` render on a *callback* response** (not just the
     sync ack) for the `[확인]`/`[중지]` buttons (which send the slash commands
     `/check` / `/stop` — the Korean label is display-only, so typing the bare
     word isn't mistaken for a poll). If they don't render, the affordance
     degrades to instructing the user to type "/check"/"/stop".
