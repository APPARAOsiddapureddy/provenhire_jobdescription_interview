import { NextResponse } from "next/server";
import { serverEnv } from "@/lib/env";

// Reads server-only config (AGENT_API_URL / INTERNAL_API_SECRET) and proxies
// to the agent; never prerender / always run on the server per request.
export const dynamic = "force-dynamic";

/**
 * POST /api/live/deepgram-token — proxy to the agent's
 * `POST ${AGENT_API_URL}/api/live/deepgram-token`. Mints a short-lived,
 * scoped Deepgram token for the browser's direct WebSocket connection (see
 * apps/web/lib/interview-session.ts).
 *
 * Unlike most proxy routes in this codebase (e.g. /api/coach/chat,
 * /api/integrity-settings), there is NO safe mock/default fallback here:
 * the interview literally cannot start without a real token, so an upstream
 * failure surfaces as a real error status instead of degrading silently.
 */
export async function POST() {
  try {
    const secret = serverEnv.internalApiSecret;
    const upstream = await fetch(
      `${serverEnv.agentApiUrl}/api/live/deepgram-token`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(secret ? { "x-internal-secret": secret } : {}),
        },
        signal: AbortSignal.timeout(15_000),
      },
    );
    if (!upstream.ok) {
      const detail = await upstream.text().catch(() => "");
      return NextResponse.json(
        { error: "Could not mint a voice session token.", detail },
        { status: 502 },
      );
    }
    const json = await upstream.json();
    return NextResponse.json(json);
  } catch {
    return NextResponse.json(
      { error: "Could not reach the voice session service." },
      { status: 502 },
    );
  }
}
