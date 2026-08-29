import { notFound } from "next/navigation";
import { serverEnv } from "@/lib/env";
import { SessionViewSchema, type SessionView } from "@/lib/session";
import { InterviewRoomClient } from "@/components/interview-room/interview-room-client";

// Reads server-only config and calls the agent API per request; never prerender.
export const dynamic = "force-dynamic";

/**
 * The session id IS the capability (OSS, no-auth design): verify it exists
 * with the agent before showing the room, same check the old LiveKit-token
 * path used before minting — there's just nothing left to mint. The
 * coordination WebSocket the room actually connects to is guarded the same
 * way (capability-guarded by this same unguessable session_id).
 */
async function loadSession(id: string): Promise<SessionView | null> {
  try {
    const res = await fetch(
      `${serverEnv.agentApiUrl}/api/session/${encodeURIComponent(id)}`,
      { cache: "no-store", signal: AbortSignal.timeout(15_000) },
    );
    if (!res.ok) return null;
    const parsed = SessionViewSchema.safeParse(await res.json());
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}

function toWsUrl(httpUrl: string): string {
  return httpUrl.replace(/^http/, "ws").replace(/\/$/, "");
}

export default async function InterviewRoomPage({
  params,
  searchParams,
}: {
  params: Promise<{ session_id: string }>;
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const { session_id } = await params;
  const sp = await searchParams;
  const candidateNameHint =
    typeof sp.candidateName === "string" ? sp.candidateName : "";
  const roleHint = typeof sp.role === "string" ? sp.role : "";
  const experienceLabel =
    typeof sp.experience === "string" ? sp.experience : undefined;

  const session = await loadSession(session_id);
  if (!session || session.session_id !== session_id) notFound();

  // Browser WebSocket needs a public-facing URL, not the docker-internal one
  const agentWsUrl =
    process.env.NEXT_PUBLIC_AGENT_API_URL || "http://localhost:8000";

  return (
    <InterviewRoomClient
      sessionId={session_id}
      candidateNameHint={candidateNameHint}
      roleHint={roleHint}
      experienceLabel={experienceLabel}
      agentWsBaseUrl={toWsUrl(agentWsUrl)}
    />
  );
}
