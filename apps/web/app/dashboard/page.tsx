import Link from "next/link";
import {
  PHButton,
  PHLogo,
  PHMetricCard,
  PHScoreBar,
  PHSectionLabel,
  PHSurface,
  PHVerdictBadge,
} from "@/components/design-system";
// Server component: scoreToVerdict must come from the plain (non-"use
// client") module — see lib/verdict.ts's docstring.
import { scoreToVerdict } from "@/lib/verdict";

/**
 * Recruiter dashboard — sample data. There is no "list every session" API
 * in this codebase yet (the agent's repository loads/saves one session at a
 * time; the report page reads a single session by id), so this screen is
 * built to spec against representative rows rather than a real query. Wiring
 * it to real data needs a new list endpoint — worth flagging as a follow-up
 * rather than silently faking a live-looking table.
 */

interface SampleRow {
  sessionId: string;
  candidate: string;
  role: string;
  questions: number;
  weaknesses: number;
  coveragePct: number; // 0-100, this row's risk/coverage score
  overallScore: number; // 0-1
}

const SAMPLE_ROWS: SampleRow[] = [
  {
    sessionId: "sess_8f2a",
    candidate: "Jordan Rivera",
    role: "Senior Backend Engineer",
    questions: 9,
    weaknesses: 1,
    coveragePct: 18,
    overallScore: 0.88,
  },
  {
    sessionId: "sess_3c91",
    candidate: "Priya Nair",
    role: "ML Engineer",
    questions: 8,
    weaknesses: 2,
    coveragePct: 34,
    overallScore: 0.71,
  },
  {
    sessionId: "sess_7be4",
    candidate: "Sam Chen",
    role: "Frontend Engineer",
    questions: 9,
    weaknesses: 3,
    coveragePct: 52,
    overallScore: 0.58,
  },
  {
    sessionId: "sess_1d0a",
    candidate: "Alex Morgan",
    role: "Platform Engineer",
    questions: 6,
    weaknesses: 4,
    coveragePct: 68,
    overallScore: 0.39,
  },
  {
    sessionId: "sess_9922",
    candidate: "Riya Kapoor",
    role: "Senior Backend Engineer",
    questions: 7,
    weaknesses: 2,
    coveragePct: 41,
    overallScore: 0.63,
  },
];

function riskColor(pct: number): string {
  if (pct < 30) return "var(--ph-green)";
  if (pct < 60) return "var(--ph-amber)";
  return "var(--ph-red)";
}

export default function DashboardPage() {
  const totalSessions = SAMPLE_ROWS.length;
  const hireCount = SAMPLE_ROWS.filter(
    (r) =>
      scoreToVerdict(r.overallScore) === "HIRE" ||
      scoreToVerdict(r.overallScore) === "STRONG_HIRE",
  ).length;
  const maybeCount = SAMPLE_ROWS.filter(
    (r) => scoreToVerdict(r.overallScore) === "MAYBE",
  ).length;
  const avgWeaknesses =
    SAMPLE_ROWS.reduce((sum, r) => sum + r.weaknesses, 0) / totalSessions;

  return (
    <main className="ph-shell min-h-screen px-6 py-10 md:px-10">
      <div className="mx-auto flex max-w-6xl flex-col gap-8">
        <header className="flex flex-wrap items-center justify-between gap-4">
          <Link href="/" className="no-underline">
            <PHLogo compact />
          </Link>
          <PHButton href="/setup" variant="primary">
            + New interview
          </PHButton>
        </header>

        <div>
          <PHSectionLabel>Recruiter dashboard</PHSectionLabel>
          <h1 className="mt-2 text-3xl font-semibold tracking-[-0.04em] text-[var(--ph-text-0)] md:text-5xl">
            Every JD interview, one floor.
          </h1>
          <p className="mt-2 max-w-xl text-[14px] leading-relaxed text-[var(--ph-text-2)]">
            Sample data below — this dashboard isn&rsquo;t wired to a real
            session list yet.
          </p>
        </div>

        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <PHMetricCard
            label="Sessions"
            value={totalSessions}
            emphasis="default"
          />
          <PHMetricCard label="Hire" value={hireCount} emphasis="green" />
          <PHMetricCard label="Maybe" value={maybeCount} emphasis="amber" />
          <PHMetricCard
            label="Avg weaknesses"
            value={avgWeaknesses.toFixed(1)}
            emphasis="blue"
          />
        </div>

        <PHSurface className="overflow-hidden rounded-2xl">
          <div className="ph-scrollbar overflow-x-auto">
            <table className="w-full min-w-[720px] border-collapse text-left">
              <thead>
                <tr className="border-b border-[var(--ph-border)]">
                  {[
                    "Session",
                    "Questions",
                    "Weaknesses",
                    "Coverage",
                    "Verdict",
                  ].map((h) => (
                    <th
                      key={h}
                      className="ph-kicker px-5 py-3 font-mono text-[var(--ph-text-3)]"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {SAMPLE_ROWS.map((row, i) => (
                  <tr
                    key={row.sessionId}
                    className={
                      i % 2 === 1 ? "bg-[var(--ph-surface-0)]" : undefined
                    }
                  >
                    <td className="px-5 py-4">
                      <Link
                        href={`/report/${encodeURIComponent(row.sessionId)}`}
                        className="text-[13.5px] font-medium text-[var(--ph-text-0)] hover:text-[var(--ph-blue)]"
                      >
                        {row.candidate}
                      </Link>
                      <p className="text-[12px] text-[var(--ph-text-3)]">
                        {row.role}
                      </p>
                    </td>
                    <td className="px-5 py-4 font-mono text-[13px] text-[var(--ph-text-1)]">
                      {row.questions}
                    </td>
                    <td className="px-5 py-4 font-mono text-[13px] text-[var(--ph-text-1)]">
                      {row.weaknesses}
                    </td>
                    <td className="w-[220px] px-5 py-4">
                      <div className="flex items-center gap-2">
                        <div className="h-2 w-full max-w-[140px] overflow-hidden rounded-full bg-[var(--ph-surface-2)]">
                          <div
                            className="h-full rounded-full"
                            style={{
                              width: `${row.coveragePct}%`,
                              background: riskColor(row.coveragePct),
                              boxShadow: `0 0 10px color-mix(in oklch, ${riskColor(row.coveragePct)} 55%, transparent)`,
                            }}
                          />
                        </div>
                        <span className="font-mono text-[11px] text-[var(--ph-text-2)]">
                          {row.coveragePct}%
                        </span>
                      </div>
                    </td>
                    <td className="px-5 py-4">
                      <PHVerdictBadge
                        verdict={scoreToVerdict(row.overallScore)}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </PHSurface>

        {/* One expanded example so PHScoreBar earns its place on this screen too. */}
        <PHSurface className="rounded-2xl p-5">
          <PHSectionLabel>Top competency spread — Jordan Rivera</PHSectionLabel>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <PHScoreBar label="System design" score={0.86} />
            <PHScoreBar label="Distributed systems" score={0.79} />
            <PHScoreBar label="Communication" score={0.9} />
            <PHScoreBar label="Ownership" score={0.62} />
          </div>
        </PHSurface>
      </div>
    </main>
  );
}
