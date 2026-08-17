import { NextResponse } from "next/server";
import {
  ProctoringEventCreateSchema,
  ProctoringEventResponseSchema,
  type ProctoringEventResponse,
} from "@proven-hire/shared";
import { serverEnv } from "@/lib/env";

// Reads server-only config (AGENT_API_URL / INTERNAL_API_SECRET) and proxies
// to the agent; never prerender / always run on the server per request.
export const dynamic = "force-dynamic";

/** A neutral response when the agent is unreachable — see POST doc below. */
function fallbackResponse(
  body: ReturnType<typeof ProctoringEventCreateSchema.parse>,
): ProctoringEventResponse {
  return {
    event: {
      ...body,
      id: crypto.randomUUID(),
      created_at: new Date().toISOString(),
    },
    score: 0,
    ban_triggered: false,
  };
}

/**
 * POST /api/proctoring-events — proxy to the agent's generic event-log
 * endpoint (`POST ${AGENT_API_URL}/api/proctoring-events`). Fire-and-forget
 * from the caller's perspective: on any upstream failure — agent down,
 * Supabase unconfigured, timeout — this returns a neutral
 * `{score: 0, ban_triggered: false}` rather than an error, so a proctoring
 * outage never blocks the interview. The tradeoff is that event silently
 * isn't logged; acceptable given `/api/integrity-settings` has the same
 * fail-open posture.
 */
export async function POST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json(
      { error: "Invalid request body" },
      { status: 400 },
    );
  }
  const parsed = ProctoringEventCreateSchema.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json(
      { error: "Invalid proctoring event payload" },
      { status: 400 },
    );
  }

  try {
    const secret = serverEnv.internalApiSecret;
    const upstream = await fetch(`${serverEnv.agentApiUrl}/api/proctoring-events`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(secret ? { "x-internal-secret": secret } : {}),
      },
      body: JSON.stringify(parsed.data),
      signal: AbortSignal.timeout(10_000),
    });
    if (!upstream.ok) {
      return NextResponse.json(fallbackResponse(parsed.data));
    }
    const json = await upstream.json();
    const parsedResponse = ProctoringEventResponseSchema.safeParse(json);
    return NextResponse.json(
      parsedResponse.success ? parsedResponse.data : fallbackResponse(parsed.data),
    );
  } catch {
    return NextResponse.json(fallbackResponse(parsed.data));
  }
}
