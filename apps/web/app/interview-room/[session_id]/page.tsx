import { notFound } from "next/navigation";
import { isLiveKitConfigured, serverEnv } from "@/lib/env";
import { createInterviewToken } from "@/lib/livekit";
import { SessionViewSchema, type SessionView } from "@/lib/session";
import { InterviewRoomClient } from "@/components/interview-room/interview-room-client";

// Reads server-only config and calls the agent API per request; never prerender.
export const dynamic = "force-dynamic";

/**
 * Token minting mirrors app/interview/[id]/page.tsx exactly: verify the
 * session exists with the agent, then mint a token pinned to that exact
 * room — never a client-supplied room name. `"prep"` is joinable (the token
 * just grants room access; the client's own polling gates the room UI
 * behind real questions actually being ready), matching the existing
 * pattern so a candidate isn't stuck waiting through two separate readiness
 * checks.
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

  let token: string | null = null;
  let url: string | null = null;
  if (isLiveKitConfigured()) {
    // The session id IS the capability (OSS, no-auth design): verify it
    // exists before minting, and pin the room to exactly this session id.
    const session = await loadSession(session_id);
    if (!session || session.session_id !== session_id) notFound();

    const JOINABLE = new Set(["prep", "ready"]);
    if (JOINABLE.has(session.status)) {
      const minted = await createInterviewToken({
        room: session.session_id,
        identity: `dev-${session_id}`,
        metadata: { session_id: session.session_id },
      });
      token = minted.token;
      url = minted.url;
    }
  }

  return (
    <InterviewRoomClient
      sessionId={session_id}
      candidateNameHint={candidateNameHint}
      roleHint={roleHint}
      experienceLabel={experienceLabel}
      token={token}
      url={url}
    />
  );
}
