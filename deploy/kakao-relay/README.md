# kakao-relay — KakaoTalk skill relay (Cloudflare Worker)

The stateless public front door for the suite's **KakaoTalk chatbot
channel**. KakaoTalk's skill model pushes a webhook the bot must answer
in **~5 s** and only lets it reply later over a single-use, ~1-minute
`callbackUrl` — neither of which the egress-only agent can serve
directly. This Worker absorbs the 5 s ack and hands each turn to a
Cloudflare Queue the agent **pulls** from, so the agent never opens an
inbound port.

Full design: [`../../docs/design/kakao-channel.md`](../../docs/design/kakao-channel.md)
(§4 the relay, §12 the secret split).

## What it does

For each skill webhook (`POST`):

1. **Authenticate** Kakao via a constant-time check of the
   `X-Kakao-Skill-Secret` header against `KAKAO_SKILL_SECRET`.
2. **Ack in 5 s** with `{"version":"2.0","useCallback":true,"data":{"text":"처리 중이에요…"}}`.
3. **Enqueue** `{chat_id, utterance, image_url, callback_url, received_at}`
   on the `KAKAO_JOBS` queue (producer binding).

It holds no business logic, no DB, and no router reach. Its only secret
gates *enqueue*, not data.

## Deploy

> For a full click-through of R2 + Queue + Worker in the Cloudflare
> dashboard (and how each value maps to a `SUITE_KAKAO_*` env var), see
> [`SETUP.md`](./SETUP.md). The CLI quickstart below covers the Worker +
> queue; Queues runs on the Workers **Free** plan (no paid plan required).

```sh
npm install

# One-time: create the queue the agent consumes from.
npx wrangler queues create kakao-jobs

# The shared secret Kakao sends in X-Kakao-Skill-Secret (also configured
# on the agent's relay — keep them equal). Never commit it.
npx wrangler secret put KAKAO_SKILL_SECRET

npx wrangler deploy
```

Then in the **Kakao i Open Builder** skill settings:

- Point the skill server at this Worker's URL.
- Add the `X-Kakao-Skill-Secret` header with the same value.
- **Enable callback** on the skill (the relay replies `useCallback: true`
  for every turn).

## Agent side

The agent consumes this queue with the matching credentials (it never
sees `KAKAO_SKILL_SECRET`):

```
SUITE_KAKAO_CF_ACCOUNT_ID=...
SUITE_KAKAO_CF_QUEUE_ID=...      # the kakao-jobs queue id
SUITE_KAKAO_CF_API_TOKEN=...     # scoped to Queues pull + ack
```

With those set (and `SUITE_VALKEY_URL`), the chatbot agent launches its
KakaoTalk pull consumer; unset, nothing Kakao-related runs.

## Verify before production

A few Kakao / Cloudflare specifics are flagged in the design doc §16 and
should be confirmed against current docs: the `callbackUrl` field path
and that callback mode is enabled, the inbound-image field shape, and the
exact CF Queues HTTP pull/ack request/response shape. The code centralizes
each so a correction is a one-line edit.
