/**
 * KakaoTalk skill relay (Cloudflare Worker).
 *
 * The only public surface of the KakaoTalk channel. For each skill
 * webhook it (1) authenticates Kakao via a shared secret, (2) returns the
 * 5s "working…" ack with `useCallback: true`, and (3) enqueues the turn
 * on the KAKAO_JOBS queue the egress-only agent pulls from. It inspects
 * nothing else and reaches nothing private — leaking its one secret lets
 * an attacker enqueue junk turns (still gated by per-user registration
 * before any dispatch), not read history or reach the control plane.
 *
 * See ../../docs/design/kakao-channel.md §4, §12, §16.
 */

export interface Env {
  KAKAO_JOBS: Queue;
  KAKAO_SKILL_SECRET: string;
}

interface KakaoJob {
  chat_id: string | undefined;
  utterance: string;
  image_url: string | null;
  callback_url: string | undefined;
  received_at: number;
}

/** Constant-time string compare, so the secret check leaks no timing. */
function timingSafeEqualStr(a: string, b: string): boolean {
  const enc = new TextEncoder();
  const ab = enc.encode(a);
  const bb = enc.encode(b);
  if (ab.length !== bb.length) return false;
  let diff = 0;
  for (let i = 0; i < ab.length; i++) diff |= ab[i] ^ bb[i];
  return diff === 0;
}

/**
 * Pull the first inbound image url out of a skill payload, if any.
 * Kakao's exact field path is verified against current docs (design
 * §16); extraction is kept tolerant so a shape mismatch degrades to
 * text-only rather than dropping the turn.
 */
function extractImage(p: any): string | null {
  const params = p?.action?.params ?? {};
  const direct = params?.image_url ?? params?.imageUrl;
  if (typeof direct === "string") return direct;
  const media = p?.userRequest?.params?.media?.url;
  if (typeof media === "string") return media;
  return null;
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    if (req.method !== "POST") return new Response("ok"); // health probe

    const got = req.headers.get("X-Kakao-Skill-Secret") ?? "";
    if (!timingSafeEqualStr(got, env.KAKAO_SKILL_SECRET)) {
      return new Response("forbidden", { status: 403 });
    }

    let p: any;
    try {
      p = await req.json();
    } catch {
      return new Response("bad request", { status: 400 });
    }

    const chatId = p?.userRequest?.user?.id;
    const callbackUrl = p?.userRequest?.callbackUrl; // ⚠ verify field (design §16)
    if (!chatId || !callbackUrl) {
      // No user id / callback → the agent couldn't route or reply; don't
      // enqueue a guaranteed-dead job. This is an IMMEDIATE skill response
      // (no callback), so it uses the normal `template.outputs` shape.
      return Response.json({
        version: "2.0",
        template: { outputs: [{ simpleText: { text: "처리할 수 없는 요청이에요." } }] },
      });
    }

    const job: KakaoJob = {
      chat_id: chatId,
      utterance: p?.userRequest?.utterance ?? "",
      image_url: extractImage(p),
      callback_url: callbackUrl,
      received_at: Date.now(),
    };
    await env.KAKAO_JOBS.send(job);

    // The useCallback ACK — defers the real answer to a later POST on
    // callbackUrl. Per Kakao's callback guide this shape uses
    // `useCallback: true` + `data.text` (the interim "working" bubble) and
    // MUST NOT include `template` — that's only for an immediate response
    // (the no-callback fallback above) and for the callback POST itself
    // (the agent's post_callback). `useCallback` on every turn so the relay
    // never needs to tell a command from a question; requires callback
    // enabled on the skill (design §4).
    return Response.json({
      version: "2.0",
      useCallback: true,
      data: { text: "처리 중이에요…" },
    });
  },
};
