import { NextResponse } from "next/server";
import { randomUUID } from "node:crypto";
import { z } from "zod";
import { gateRequest } from "@proven-hire/ee";
import { presignUpload } from "@/lib/r2";
import { isR2Configured } from "@/lib/env";
import { getUser } from "@/lib/supabase/server";

// Screen-recording uploads only, for Integrity Controls' "Screen Recording
// Enabled". Deliberately a SEPARATE route from /api/upload (CV files) — a
// different mime allowlist and a much larger size ceiling, so a bug in one
// can't accidentally widen the other.
const ALLOWED_CONTENT_TYPES = new Set(["video/webm", "video/mp4"]);

// A full interview screen recording can run tens of minutes; 500MB is
// generous headroom without inviting storage abuse.
const MAX_UPLOAD_BYTES = 500 * 1024 * 1024;

const BodySchema = z.object({
  session_id: z.string().min(1),
  filename: z.string().min(1),
  content_type: z.string().min(1),
  size: z.number().int().positive().max(MAX_UPLOAD_BYTES),
});

export async function POST(request: Request) {
  // Same distribution gate as /api/upload — a screen recording is storage
  // cost too, so a required-auth distribution should reject anonymous callers.
  const user = await getUser();
  const gate = gateRequest({
    pathname: "/api/recordings",
    isAuthenticated: Boolean(user),
  });
  if (!gate.allow) {
    return NextResponse.json({ error: "Sign in required" }, { status: 401 });
  }

  let body: z.infer<typeof BodySchema>;
  try {
    body = BodySchema.parse(await request.json());
  } catch {
    return NextResponse.json(
      { error: "Invalid request body" },
      { status: 400 },
    );
  }

  const normalizedType = (body.content_type.split(";", 1)[0] ?? "")
    .trim()
    .toLowerCase();
  if (!ALLOWED_CONTENT_TYPES.has(normalizedType)) {
    return NextResponse.json(
      { error: "Unsupported recording type. Expected webm or mp4." },
      { status: 415 },
    );
  }

  if (!isR2Configured()) {
    return NextResponse.json({ error: "R2 not configured" }, { status: 501 });
  }

  // Random, unguessable key segment, scoped under the session id for easy
  // operator lookup — never trust the client-supplied filename as the key.
  const safeName = body.filename.replace(/[/\\]/g, "_").slice(0, 100);
  const key = `recordings/${encodeURIComponent(body.session_id)}/${randomUUID()}-${safeName}`;

  const result = await presignUpload(key, body.content_type, body.size);
  return NextResponse.json(result);
}
