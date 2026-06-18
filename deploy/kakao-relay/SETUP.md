# KakaoTalk channel — Cloudflare setup (dashboard)

Step-by-step to provision the three Cloudflare pieces the channel needs —
an **R2 bucket** (outbound images), a **Queue** (`kakao-jobs`), and the
**Worker relay** — and to wire their values into the agent's
`SUITE_KAKAO_*` env. Architecture: [`../../docs/design/kakao-channel.md`](../../docs/design/kakao-channel.md).

```
KakaoTalk ──webhook──▶ Worker relay ──produce──▶ Queue (kakao-jobs)
                          (this dir)                  ▲ HTTP pull + ack
                                              agent (egress-only) ──▶ R2 (presigned image URLs)
```

## Plan / cost

- **Cloudflare Queues runs on the Workers _Free_ plan** (since 2026‑02‑04):
  ~10,000 queue operations/day and **24h** max message retention (vs 14
  days on paid); HTTP pull consumers and up to 10,000 queues are included.
  This channel's volume sits well under that — messages are acked within
  seconds, far below the 24h cap. A paid plan is **not** required.
- **R2** has a free tier (storage + Class A/B operations); enable R2 once.
- Both require **billing activated** on the account (no card charge to use
  the free allowances).

## A. Account ID

Dashboard → **Workers & Pages** → right sidebar **Account ID**. Copy it →
`SUITE_KAKAO_CF_ACCOUNT_ID`, and the R2 endpoint host.

## B. R2 bucket + S3 credentials (outbound images — optional)

Skip this section for a text-only deploy; inbound images still work via the
router file store, and the channel runs without R2.

1. Dashboard → **R2** → **Create bucket** → e.g. `kakao-images` → **Create**.
   Leave it **private** — Kakao fetches images via short-lived *presigned*
   URLs, which work on private buckets (do NOT enable public access).
2. R2 → **Manage R2 API Tokens** → **Create API token**:
   - Permission **Object Read & Write**, scoped to the `kakao-images` bucket.
   - Create, then copy the **Access Key ID** and **Secret Access Key**
     (shown once).
3. S3 endpoint: `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`.

| Value | Env var |
|---|---|
| `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` | `SUITE_KAKAO_R2_ENDPOINT_URL` |
| `kakao-images` | `SUITE_KAKAO_R2_BUCKET` |
| Access Key ID | `SUITE_KAKAO_R2_ACCESS_KEY_ID` |
| Secret Access Key | `SUITE_KAKAO_R2_SECRET_ACCESS_KEY` |

The agent signs with region `auto` (R2's convention) automatically.

## C. Queue + HTTP pull consumer

1. Dashboard → **Workers & Pages → Queues** → **Create queue** →
   name `kakao-jobs` → **Create**.
2. Open `kakao-jobs` → copy the **Queue ID** → `SUITE_KAKAO_CF_QUEUE_ID`.
3. On the queue → **Consumers** → add an **HTTP Pull** consumer (NOT a
   Worker consumer). This is what lets the egress-only agent pull over
   HTTPS. If the dashboard doesn't surface it, use the CLI:
   `npx wrangler queues consumer http add kakao-jobs`.

The **producer** side (the Worker writing to the queue) is configured in §E.

## D. API token for Queues pull/ack

Dashboard → **My Profile → API Tokens** → **Create Token → Create Custom
Token**:
- Permission **Account → Queues → Edit** (covers both pull and ack).
- Apply to your account.
- Create → copy → `SUITE_KAKAO_CF_API_TOKEN`.

## E. Deploy the Worker relay

**Recommended — wrangler** (this dir already has `wrangler.toml` with the
producer binding + queue name):

```sh
cd deploy/kakao-relay
npm install
npx wrangler login
npx wrangler secret put KAKAO_SKILL_SECRET   # paste the secret from §F
npx wrangler deploy
```

`wrangler.toml` binds the producer:

```toml
[[queues.producers]]
queue = "kakao-jobs"
binding = "KAKAO_JOBS"
```

Copy the deployed URL (`https://kakao-relay.<subdomain>.workers.dev`).

**Dashboard alternative** (no CLI): Workers & Pages → **Create application →
Worker** → name `kakao-relay` → deploy → **Edit code** and paste
`src/worker.ts` (strip the TS type annotations if the editor objects) →
**Deploy**. Then:
- **Settings → Bindings → Add → Queue Producer**: variable `KAKAO_JOBS`,
  queue `kakao-jobs`.
- **Settings → Variables and Secrets → Add → Secret**:
  `KAKAO_SKILL_SECRET` = the secret from §F. Redeploy.

## F. The skill secret

A high-entropy string authenticating Kakao→relay; lives only on the Worker
(never on the agent):

```sh
openssl rand -hex 32
```

Use the same value as the Worker's `KAKAO_SKILL_SECRET` (§E) and Kakao's
request header (§H).

## G. Wire the agent (`deploy/.env.prod`)

```sh
SUITE_KAKAO_CF_ACCOUNT_ID=<A>
SUITE_KAKAO_CF_QUEUE_ID=<C.2>
SUITE_KAKAO_CF_API_TOKEN=<D>
# Outbound images (optional — omit for text-only):
SUITE_KAKAO_R2_ENDPOINT_URL=https://<A>.r2.cloudflarestorage.com
SUITE_KAKAO_R2_BUCKET=kakao-images
SUITE_KAKAO_R2_ACCESS_KEY_ID=<B.2>
SUITE_KAKAO_R2_SECRET_ACCESS_KEY=<B.2>
# SUITE_VALKEY_URL already defaults to the in-cluster redis in compose.
```

Restart the chatbot. On boot it logs `kakao_consumer_started`. The three
`SUITE_KAKAO_CF_*` vars gate the channel; the R2 vars are optional.

## H. Kakao i Open Builder

In the skill settings: set the **skill server URL** to the Worker URL; add
header `X-Kakao-Skill-Secret: <F>`; and **enable callback** on the skill
(the relay replies `useCallback: true` for every turn).

## I. Verify

- Message the bot → "처리 중이에요…" within ~5s (relay ack), then the real
  answer on the callback.
- Worker logs: the Worker → **Logs**, or `npx wrangler tail`.
- Queue throughput: the `kakao-jobs` **Metrics** tab.
- Agent logs: `kakao_consumer_started`, then per-turn processing events.

## Verify-before-prod (design §16)

External shapes the code centralizes (so a mismatch is a one-line fix):
the Kakao `callbackUrl` field path + its TTL (tune `SUITE_KAKAO_CALLBACK_TTL_S`
/ `SUITE_KAKAO_CALLBACK_DEADLINE_S`), and the CF Queues HTTP pull/ack
request/response shape (`kakao_client.pull`/`ack`).

Sources for the Queues-on-Free change:
- https://developers.cloudflare.com/changelog/post/2026-02-04-queues-free-plan/
- https://developers.cloudflare.com/queues/platform/pricing/
