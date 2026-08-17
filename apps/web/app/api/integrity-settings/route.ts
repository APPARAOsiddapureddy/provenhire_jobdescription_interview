import { NextResponse } from "next/server";
import {
  DEFAULT_INTEGRITY_SETTINGS,
  IntegritySettingsSchema,
} from "@proven-hire/shared";
import { serverEnv } from "@/lib/env";

// Reads server-only config (AGENT_API_URL / INTERNAL_API_SECRET) and proxies
// to the agent; never prerender / always run on the server per request.
export const dynamic = "force-dynamic";

/**
 * GET /api/integrity-settings — proxy to the agent's
 * `GET ${AGENT_API_URL}/api/integrity-settings`.
 *
 * Called both by the `/settings/integrity` admin page (to render current
 * values) and by the live interview room at session start (to know which
 * checks to enforce). On any failure — agent down, Supabase unconfigured,
 * offline dev — falls back to all-OFF defaults rather than an error, so a
 * misconfigured/unreachable settings store never blocks an interview.
 */
export async function GET() {
  try {
    const secret = serverEnv.internalApiSecret;
    const upstream = await fetch(
      `${serverEnv.agentApiUrl}/api/integrity-settings`,
      {
        method: "GET",
        headers: {
          accept: "application/json",
          ...(secret ? { "x-internal-secret": secret } : {}),
        },
        cache: "no-store",
        signal: AbortSignal.timeout(10_000),
      },
    );
    if (!upstream.ok) {
      return NextResponse.json(DEFAULT_INTEGRITY_SETTINGS);
    }
    const json = await upstream.json();
    const parsed = IntegritySettingsSchema.safeParse(json);
    return NextResponse.json(
      parsed.success ? parsed.data : DEFAULT_INTEGRITY_SETTINGS,
    );
  } catch {
    return NextResponse.json(DEFAULT_INTEGRITY_SETTINGS);
  }
}

/**
 * PUT /api/integrity-settings — the settings page's save action. Proxies to
 * the agent with the internal secret attached (same pattern as
 * `/api/coach/chat`); the agent is the only thing holding Supabase
 * service-role credentials, so this route never talks to Postgres directly.
 *
 * No auth gate here (open settings page, per this build's auth-free-by-default
 * posture) — anyone who can reach this URL can change global proctoring
 * config. Acceptable for self-host; add a `gateRequest`-style check later if
 * a distribution introduces roles.
 */
export async function PUT(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json(
      { error: "Invalid request body" },
      { status: 400 },
    );
  }
  const parsed = IntegritySettingsSchema.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json(
      { error: "Invalid settings payload" },
      { status: 400 },
    );
  }

  try {
    const secret = serverEnv.internalApiSecret;
    const upstream = await fetch(
      `${serverEnv.agentApiUrl}/api/integrity-settings`,
      {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          ...(secret ? { "x-internal-secret": secret } : {}),
        },
        body: JSON.stringify(parsed.data),
        signal: AbortSignal.timeout(10_000),
      },
    );
    if (!upstream.ok) {
      return NextResponse.json(
        { error: "Could not reach the settings service." },
        { status: 502 },
      );
    }
    const json = await upstream.json();
    const parsedResponse = IntegritySettingsSchema.safeParse(json);
    return NextResponse.json(
      parsedResponse.success ? parsedResponse.data : parsed.data,
    );
  } catch {
    return NextResponse.json(
      { error: "Could not reach the settings service." },
      { status: 502 },
    );
  }
}
