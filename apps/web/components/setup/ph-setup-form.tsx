"use client";

/**
 * <PHSetupForm> — the ProvenHire-branded JD-intake screen.
 *
 * Collects a real resume (upload or paste) AND a job description — the prep
 * pipeline's gap analysis needs both to be meaningful (JD-derived questions,
 * but scored against what the CANDIDATE actually brings). An earlier version
 * of this screen dropped the resume field entirely and synthesized a thin
 * placeholder blurb instead; that measurably weakened gap analysis, so real
 * upload/paste is back, using the exact same real logic (R2 presign, or a
 * base64 data-URL fallback so the agent still parses the real PDF/DOCX when
 * R2 isn't configured) as the original setup form.
 */

import { useRouter } from "next/navigation";
import { useCallback, useRef, useState } from "react";
import type { FollowUpDepth, LanguageMode } from "@proven-hire/shared";
import { startSession } from "@/app/setup/actions";
import { cn } from "@/lib/cn";
import {
  PHButton,
  PHChip,
  PHSectionLabel,
  PHSurface,
} from "@/components/design-system";

const YEARS_OPTIONS = ["0-1", "1-2", "2-4", "4-6", "6+"] as const;
type YearsBucket = (typeof YEARS_OPTIONS)[number];

const FOLLOW_UP_DEPTH_OPTIONS: {
  value: FollowUpDepth;
  label: string;
  detail: string;
}[] = [
  {
    value: "light",
    label: "Light",
    detail: "Mostly scripted questions, minimal probing.",
  },
  {
    value: "moderate",
    label: "Moderate",
    detail: "A balanced mix — the default.",
  },
  {
    value: "deep",
    label: "Deep",
    detail: "Digs in with more follow-ups per question.",
  },
];

const STAGES = [
  {
    key: "role-fit",
    title: "Stage 1 — Role Fit",
    detail: "Warm-up + how your background maps to the role.",
  },
  {
    key: "core-skills",
    title: "Stage 2 — Core Skills from JD",
    detail: "Technical depth on what the job description actually asks for.",
  },
  {
    key: "scenario",
    title: "Stage 3 — Scenario / System Thinking",
    detail: "Open-ended problem you'd realistically hit in the role.",
  },
] as const;

const MIN_JD_CHARS = 40;
const MIN_CV_CHARS = 30;
// Matches the /api/upload ceiling; also keeps the no-R2 data-URL fallback
// (base64 is ~+33%) under the Next server-action body limit.
const MAX_CV_BYTES = 10 * 1024 * 1024;

// One-click sample JD + CV for fast testing/demos — the old setup form had
// this ("Quick demo"); dropping it would be a real usability regression.
const SAMPLES = [
  {
    id: "backend",
    label: "Backend Engineer · Stripe",
    role: "Senior Backend Engineer",
    name: "Jordan Rivera",
    email: "jordan.rivera@example.com",
    jd: "Senior Backend Engineer to build payment APIs at scale. Own services in Python/Go, design event-driven systems with Kafka, and ensure reliability, idempotency, and low p99 latency on PostgreSQL. Strong distributed-systems background required.",
    cv: "Jordan Rivera — Senior Backend Engineer. 7 years building high-throughput payment and API platforms. Led a ledger service at 5k req/s; cut p99 latency 40% with async batching; owned idempotency and reconciliation. Skills: Python, Go, PostgreSQL, Kafka, gRPC, Kubernetes, distributed systems.",
  },
  {
    id: "frontend",
    label: "Frontend Engineer · Vercel",
    role: "Senior Frontend Engineer",
    name: "Sam Chen",
    email: "sam.chen@example.com",
    jd: "Senior Frontend Engineer to build fast, delightful web experiences with Next.js and React. Own component architecture, Core Web Vitals, accessibility, and design-system work. Deep TypeScript and modern rendering (SSR/streaming) experience expected.",
    cv: "Sam Chen — Senior Frontend Engineer. 6 years shipping performant React/Next.js apps. Built a design system used across 30+ surfaces; improved LCP 35% with streaming SSR and image optimization. Skills: TypeScript, React, Next.js, Tailwind, accessibility, Core Web Vitals.",
  },
  {
    id: "ml",
    label: "ML Engineer · OpenAI",
    role: "Machine Learning Engineer",
    name: "Priya Nair",
    email: "priya.nair@example.com",
    jd: "Machine Learning Engineer to build and ship production ML systems: data pipelines, training, evaluation, and low-latency serving. You will own offline/online metrics, monitoring, and safe rollouts. Strong Python + PyTorch and MLOps experience required.",
    cv: "Priya Nair — Machine Learning Engineer. 5 years in production ML: built feature pipelines and model serving for ranking at scale; owned offline/online evaluation and safe rollback. Skills: Python, PyTorch, Ray, feature stores, evaluation, MLOps, monitoring.",
  },
] as const;

const inputClass =
  "w-full rounded-xl border border-[var(--ph-border)] bg-[var(--ph-surface-0)] px-4 py-3 text-sm text-[var(--ph-text-0)] placeholder:text-[var(--ph-text-3)] outline-none transition-all focus:border-[var(--ph-border-strong)] focus:bg-[var(--ph-surface-1)]";

type LoadingStage = "permissions" | "preparing" | "starting" | null;

const LOADING_LABEL: Record<Exclude<LoadingStage, null>, string> = {
  permissions: "Checking mic & camera access…",
  preparing: "Reading your resume and the job description…",
  starting: "Starting your interview…",
};

export function PHSetupForm({ r2Configured }: { r2Configured: boolean }) {
  const router = useRouter();
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const [file, setFile] = useState<File | null>(null);
  const [cvText, setCvText] = useState("");
  const [dragging, setDragging] = useState(false);

  const [jdText, setJdText] = useState("");
  const [role, setRole] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [years, setYears] = useState<YearsBucket>("2-4");
  const [followUpDepth, setFollowUpDepth] = useState<FollowUpDepth>("moderate");

  const [cvTouched, setCvTouched] = useState(false);
  const [jdTouched, setJdTouched] = useState(false);
  const [loading, setLoading] = useState<LoadingStage>(null);
  const [error, setError] = useState<string | null>(null);

  // CV is satisfied by a chosen file (length unknown synchronously) OR
  // pasted text of at least MIN_CV_CHARS.
  const cvLen = cvText.trim().length;
  const cvError = !file
    ? cvLen === 0
      ? "Upload or paste your resume to continue."
      : cvLen < MIN_CV_CHARS
        ? `Add a bit more — this looks too short (at least ${MIN_CV_CHARS} characters).`
        : null
    : file.size > MAX_CV_BYTES
      ? `That file is too large (max ${Math.floor(MAX_CV_BYTES / (1024 * 1024))} MB). Upload a smaller file or paste the text.`
      : null;
  const jdLen = jdText.trim().length;
  const jdError =
    jdLen === 0
      ? "Paste the job description to continue."
      : jdLen < MIN_JD_CHARS
        ? `Paste the full posting — this looks too short (at least ${MIN_JD_CHARS} characters).`
        : null;
  const nameError = name.trim().length === 0 ? "Add your name." : null;
  const canSubmit = !cvError && !jdError && !nameError && loading === null;

  function loadSample(s: (typeof SAMPLES)[number]) {
    setFile(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
    setCvText(s.cv);
    setJdText(s.jd);
    setRole(s.role);
    setName(s.name);
    setEmail(s.email);
    setCvTouched(false);
    setJdTouched(false);
    setError(null);
  }

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const dropped = e.dataTransfer.files?.[0];
    if (dropped) setFile(dropped);
  }, []);

  /**
   * Read a file as a base64 `data:` URL of its RAW bytes (no R2 configured).
   * The agent base64-decodes this and parses the real document (PDF/DOCX) —
   * unlike `file.text()`, which mangles binary formats into garbage.
   */
  function fileToDataUrl(f: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as string);
      reader.onerror = () =>
        reject(reader.error ?? new Error("Could not read file."));
      reader.readAsDataURL(f);
    });
  }

  /** Upload the chosen file to R2 (presign → PUT) and return its public URL. */
  async function uploadToR2(f: File): Promise<string> {
    const res = await fetch("/api/upload", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        filename: f.name,
        content_type: f.type || "application/octet-stream",
        size: f.size,
      }),
    });
    if (!res.ok) throw new Error("Upload could not be prepared.");
    const { uploadUrl, publicUrl } = (await res.json()) as {
      uploadUrl: string;
      publicUrl: string;
    };
    const put = await fetch(uploadUrl, {
      method: "PUT",
      headers: { "content-type": f.type || "application/octet-stream" },
      body: f,
    });
    if (!put.ok) throw new Error("File upload failed.");
    return publicUrl;
  }

  async function requestMediaPermissions() {
    if (
      typeof navigator === "undefined" ||
      !navigator.mediaDevices?.getUserMedia
    ) {
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: true,
        video: true,
      });
      stream.getTracks().forEach((t) => t.stop());
    } catch {
      // Non-blocking: the interview room degrades gracefully without
      // camera/mic access (same philosophy as <DeviceCheck>).
    }
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    if (cvError || jdError || nameError) {
      setCvTouched(true);
      setJdTouched(true);
      return;
    }

    setLoading("permissions");
    await requestMediaPermissions();

    setLoading("preparing");
    try {
      // Same resolution order as the original setup form: uploaded file to
      // R2 when configured; pasted text as-is; otherwise the raw file bytes
      // as a base64 data URL so the agent still parses the real document.
      let cv_url: string;
      if (file && r2Configured) {
        cv_url = await uploadToR2(file);
      } else if (cvText.trim()) {
        cv_url = cvText.trim();
      } else if (file) {
        cv_url = await fileToDataUrl(file);
      } else {
        cv_url = "";
      }

      const language_mode: LanguageMode = { primary: "en", mixed: false };
      const result = await startSession({
        cv_url,
        jd_text: jdText.trim(),
        company: "",
        language_mode,
        follow_up_depth: followUpDepth,
      });

      if (!result.ok) {
        if (result.reason === "auth_required") {
          router.push("/login?next=/setup");
          return;
        }
        setError(result.error);
        setLoading(null);
        return;
      }

      setLoading("starting");
      const params = new URLSearchParams({
        candidateName: name.trim(),
        role: role.trim() || "this role",
        experience: years,
      });
      router.push(`/interview-room/${result.session_id}?${params.toString()}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
      setLoading(null);
    }
  }

  if (loading) {
    return (
      <PHSurface className="mt-8 flex flex-col items-center gap-4 rounded-2xl px-8 py-14 text-center">
        <span
          className="h-6 w-6 animate-spin rounded-full border-2 border-[var(--ph-blue)] border-t-transparent"
          aria-hidden
        />
        <p className="text-[15px] font-medium text-[var(--ph-text-0)]">
          {LOADING_LABEL[loading]}
        </p>
        {error && (
          <p className="text-[13px] text-[var(--ph-red)]" role="alert">
            {error}
          </p>
        )}
      </PHSurface>
    );
  }

  return (
    <form onSubmit={onSubmit} className="mt-8 flex flex-col gap-6">
      <div>
        <PHSectionLabel>JD intake</PHSectionLabel>
        <h1 className="mt-2 text-3xl font-semibold tracking-[-0.03em] text-[var(--ph-text-0)] md:text-4xl">
          Bring your resume. Paste the job.
        </h1>
        <p className="mt-2 max-w-xl text-[14px] leading-relaxed text-[var(--ph-text-2)]">
          ProvenHire reads both, runs a real gap analysis, and builds a voice
          interview from what the role actually asks for — three stages, scored
          against what you actually bring.
        </p>
      </div>

      <PHSurface className="flex flex-col gap-3 rounded-2xl p-5 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-[13px] font-medium text-[var(--ph-text-1)]">
            Quick demo
          </p>
          <p className="text-[12px] text-[var(--ph-text-3)]">
            Load a matched sample resume + JD to try it fast.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {SAMPLES.map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => loadSample(s)}
              className="rounded-lg border border-[var(--ph-border)] px-3 py-1.5 text-[12px] text-[var(--ph-text-2)] transition-colors hover:border-[var(--ph-border-strong)] hover:text-[var(--ph-text-0)]"
            >
              {s.label}
            </button>
          ))}
        </div>
      </PHSurface>

      {/* Resume */}
      <PHSurface className="flex flex-col gap-4 rounded-2xl p-5">
        <label className="text-[13px] font-medium text-[var(--ph-text-1)]">
          Resume
        </label>
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          onClick={() => fileInputRef.current?.click()}
          className={cn(
            "flex cursor-pointer flex-col items-center gap-2 rounded-xl border border-dashed px-4 py-8 text-center transition-colors",
            dragging
              ? "border-[var(--ph-blue)] bg-[var(--ph-blue-soft)]"
              : "border-[var(--ph-border)] hover:border-[var(--ph-border-strong)]",
          )}
        >
          {file ? (
            <span className="flex items-center gap-2 text-[14px] text-[var(--ph-text-0)]">
              {file.name}
              <button
                type="button"
                aria-label="Remove file"
                onClick={(e) => {
                  e.stopPropagation();
                  setFile(null);
                  if (fileInputRef.current) fileInputRef.current.value = "";
                }}
                className="text-[var(--ph-text-2)] hover:text-[var(--ph-text-0)]"
              >
                ✕
              </button>
            </span>
          ) : (
            <span className="text-[13px] text-[var(--ph-text-2)]">
              Drop your resume here, or click to browse (PDF or DOCX)
            </span>
          )}
          <input
            ref={fileInputRef}
            type="file"
            accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            className="hidden"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </div>

        <div>
          <label
            htmlFor="cvText"
            className="text-[13px] font-medium text-[var(--ph-text-1)]"
          >
            …or paste your resume text
          </label>
          <textarea
            id="cvText"
            rows={5}
            className={cn(inputClass, "mt-2")}
            placeholder="Paste your resume text here…"
            value={cvText}
            onChange={(e) => setCvText(e.target.value)}
            onBlur={() => setCvTouched(true)}
            aria-invalid={cvTouched && Boolean(cvError)}
          />
        </div>
        {cvTouched && cvError && (
          <p className="text-[13px] text-[var(--ph-red)]" role="alert">
            {cvError}
          </p>
        )}
      </PHSurface>

      <PHSurface className="flex flex-col gap-2 rounded-2xl p-5">
        <label
          htmlFor="jdText"
          className="text-[13px] font-medium text-[var(--ph-text-1)]"
        >
          Job description
        </label>
        <textarea
          id="jdText"
          rows={8}
          className={inputClass}
          placeholder="Paste the full job posting here…"
          value={jdText}
          onChange={(e) => setJdText(e.target.value)}
          onBlur={() => setJdTouched(true)}
          aria-invalid={jdTouched && Boolean(jdError)}
        />
        {jdTouched && jdError && (
          <p className="text-[13px] text-[var(--ph-red)]" role="alert">
            {jdError}
          </p>
        )}
      </PHSurface>

      <div className="grid gap-4 sm:grid-cols-2">
        <PHSurface className="flex flex-col gap-2 rounded-2xl p-5">
          <label
            htmlFor="role"
            className="text-[13px] font-medium text-[var(--ph-text-1)]"
          >
            Target role / title
          </label>
          <input
            id="role"
            className={inputClass}
            placeholder="Senior Backend Engineer"
            value={role}
            onChange={(e) => setRole(e.target.value)}
          />
        </PHSurface>

        <PHSurface className="flex flex-col gap-2 rounded-2xl p-5">
          <label className="text-[13px] font-medium text-[var(--ph-text-1)]">
            Years of experience
          </label>
          <div className="flex flex-wrap gap-2 pt-1">
            {YEARS_OPTIONS.map((y) => (
              <PHChip
                key={y}
                type="button"
                active={years === y}
                onClick={() => setYears(y)}
              >
                {y} yrs
              </PHChip>
            ))}
          </div>
        </PHSurface>

        <PHSurface className="flex flex-col gap-2 rounded-2xl p-5">
          <label className="text-[13px] font-medium text-[var(--ph-text-1)]">
            Follow-up depth
          </label>
          <div className="flex flex-wrap gap-2 pt-1">
            {FOLLOW_UP_DEPTH_OPTIONS.map((d) => (
              <PHChip
                key={d.value}
                type="button"
                active={followUpDepth === d.value}
                onClick={() => setFollowUpDepth(d.value)}
              >
                {d.label}
              </PHChip>
            ))}
          </div>
          <p className="text-[12px] text-[var(--ph-text-3)]">
            {
              FOLLOW_UP_DEPTH_OPTIONS.find((d) => d.value === followUpDepth)
                ?.detail
            }
          </p>
        </PHSurface>

        <PHSurface className="flex flex-col gap-2 rounded-2xl p-5">
          <label
            htmlFor="name"
            className="text-[13px] font-medium text-[var(--ph-text-1)]"
          >
            Candidate name
          </label>
          <input
            id="name"
            className={inputClass}
            placeholder="Jordan Rivera"
            value={name}
            onChange={(e) => setName(e.target.value)}
            aria-invalid={jdTouched && Boolean(nameError)}
          />
        </PHSurface>

        <PHSurface className="flex flex-col gap-2 rounded-2xl p-5">
          <label
            htmlFor="email"
            className="text-[13px] font-medium text-[var(--ph-text-1)]"
          >
            Email <span className="text-[var(--ph-text-3)]">(optional)</span>
          </label>
          <input
            id="email"
            type="email"
            className={inputClass}
            placeholder="jordan@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </PHSurface>
      </div>

      {/* Stage preview */}
      <div className="flex flex-col gap-3">
        <PHSectionLabel>What to expect</PHSectionLabel>
        <div className="grid gap-3 sm:grid-cols-3">
          {STAGES.map((s) => (
            <PHSurface key={s.key} className="rounded-2xl p-4">
              <p className="font-mono text-[11px] font-semibold uppercase tracking-[0.08em] text-[var(--ph-blue)]">
                {s.title}
              </p>
              <p className="mt-1.5 text-[12.5px] leading-relaxed text-[var(--ph-text-2)]">
                {s.detail}
              </p>
            </PHSurface>
          ))}
        </div>
      </div>

      {error && (
        <p className="text-[13px] text-[var(--ph-red)]" role="alert">
          {error}
        </p>
      )}

      <PHButton
        type="submit"
        variant="primary"
        className={cn("self-start", !canSubmit && "opacity-50")}
        disabled={!canSubmit}
        aria-disabled={!canSubmit}
      >
        Start interview
      </PHButton>

      <p className="text-[11px] text-[var(--ph-text-3)]">
        ProvenHire uses integrity monitoring to ensure fair assessments.
      </p>
    </form>
  );
}
