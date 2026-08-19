import { NextResponse } from "next/server";
import { serverEnv } from "@/lib/env";

// Reads server-only config (AGENT_API_URL / INTERNAL_API_SECRET) and proxies
// to the agent; never prerender / always run on the server per request.
export const dynamic = "force-dynamic";

/**
 * POST /api/live/tts — proxy to the agent's `POST ${AGENT_API_URL}/api/live/tts`.
 * Same secret-attaching proxy pattern as deepgram-token/route.ts, but streams
 * back audio bytes instead of JSON. Forwards the caller's own AbortSignal to
 * the upstream fetch (in addition to a 30s ceiling) so that when the browser
 * aborts for barge-in, the in-flight Cartesia call is actually cancelled too
 * — not just the local response stream.
 */
export async function POST(req: Request) {
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Invalid request body." }, { status: 400 });
  }

  try {
    const secret = serverEnv.internalApiSecret;
    const upstream = await fetch(`${serverEnv.agentApiUrl}/api/live/tts`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(secret ? { "x-internal-secret": secret } : {}),
      },
      body: JSON.stringify(body),
      signal: AbortSignal.any([req.signal, AbortSignal.timeout(30_000)]),
    });
    if (!upstream.ok) {
      const detail = await upstream.text().catch(() => "");
      return NextResponse.json(
        { error: "Could not synthesize speech.", detail },
        { status: 502 },
      );
    }
    const bytes = await upstream.arrayBuffer();
    return new NextResponse(bytes, {
      status: 200,
      headers: { "Content-Type": "audio/mpeg" },
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      // Client cancelled (barge-in) — nothing meaningful to send back.
      return new NextResponse(null, { status: 499 });
    }
    return NextResponse.json(
      { error: "Could not reach the voice session service." },
      { status: 502 },
    );
  }
}
