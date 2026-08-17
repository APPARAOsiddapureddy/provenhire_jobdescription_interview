"use client";

/**
 * ProvenHire design-system kit ("PH" atoms).
 *
 * The visual counterpart to Antigravity Interview's component library — same
 * tokens (see globals.css's `--ph-*` variables), same component shapes, same
 * motion timings, so the two products read as one family. No component
 * library is installed for this layer (no shadcn/Radix, lucide-react is not
 * wired in here) — every icon below is a small hand-drawn inline SVG, and
 * every atom is hand-rolled against the `--ph-*` tokens directly.
 *
 * This is additive: the app's original `components/ui/*` kit (Card, Button,
 * …) still backs the marketing site and the un-migrated screens. This file
 * is exclusively for the new ProvenHire-branded screens (setup, interview
 * room, dashboard, report).
 */

import * as React from "react";
import Link from "next/link";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/cn";
import { scoreToVerdict, type PHVerdict } from "@/lib/verdict";

// Re-exported for convenience so existing client-side imports of
// `PHVerdict`/`scoreToVerdict` from this file keep working — the canonical
// definitions live in lib/verdict.ts (a plain, non-"use client" module) so
// server components can call scoreToVerdict() directly too.
export { scoreToVerdict, type PHVerdict };

/* ────────────────────────────────────────────────────────────────────
   Icons — small, hand-drawn, stroke-based (currentColor), 1.5px stroke.
   ──────────────────────────────────────────────────────────────────── */

type IconProps = React.SVGProps<SVGSVGElement>;

function iconProps(props: IconProps): IconProps {
  return {
    width: 16,
    height: 16,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.75,
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": true,
    ...props,
  };
}

/** The ProvenHire mark — a hexagon with an inner chevron ("verified" motif). */
export function PHHexIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <path d="M12 2.5 21 7.5v9L12 21.5 3 16.5v-9Z" />
      <path d="M8.5 12.2 11 14.5l4.5-5" />
    </svg>
  );
}

export function PHChevronIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <path d="m9 6 6 6-6 6" />
    </svg>
  );
}

export function PHMicIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0" />
      <path d="M12 18v3" />
    </svg>
  );
}

export function PHCameraIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <rect x="3" y="6" width="13" height="12" rx="2.5" />
      <path d="m16 10.5 5-3v9l-5-3" />
    </svg>
  );
}

export function PHDotIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)} viewBox="0 0 8 8" width={8} height={8}>
      <circle cx="4" cy="4" r="4" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function PHPlusIcon(props: IconProps) {
  return (
    <svg {...iconProps(props)}>
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

/* ────────────────────────────────────────────────────────────────────
   PHLogo
   ──────────────────────────────────────────────────────────────────── */

export function PHLogo({ compact = false }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-2.5">
      <span
        className={cn(
          "flex h-9 w-9 shrink-0 items-center justify-center rounded-xl",
          "border border-[var(--ph-border-strong)] bg-[var(--ph-blue-soft)]",
          "shadow-[0_0_0_1px_var(--ph-blue-soft),0_8px_20px_-8px_var(--ph-blue)]",
        )}
      >
        <PHHexIcon width={19} height={19} className="text-[var(--ph-blue)]" />
      </span>
      {!compact && (
        <span className="flex items-center gap-2">
          <span className="font-semibold tracking-[-0.02em] text-[var(--ph-text-0)]">
            ProvenHire
          </span>
          <span className="ph-kicker rounded-md border border-[var(--ph-border)] bg-[var(--ph-surface-1)] px-1.5 py-0.5">
            INTERVIEW
          </span>
        </span>
      )}
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────────
   PHButton
   ──────────────────────────────────────────────────────────────────── */

const phButtonVariants = cva(
  "inline-flex items-center justify-center gap-2 rounded-xl px-5 py-3 text-sm font-semibold transition-all duration-200 disabled:pointer-events-none disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ph-blue)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--ph-bg)]",
  {
    variants: {
      variant: {
        primary:
          "bg-[var(--ph-text-0)] text-[oklch(0.1_0.01_260)] hover:-translate-y-0.5 hover:shadow-[0_10px_30px_-10px_oklch(0.96_0.003_260/0.4)]",
        secondary:
          "border border-[var(--ph-border)] bg-[var(--ph-surface-0)] text-[var(--ph-text-0)] hover:border-[var(--ph-border-strong)] hover:bg-[var(--ph-surface-1)]",
        ghost:
          "border border-transparent bg-transparent text-[var(--ph-text-1)] hover:border-[var(--ph-border)] hover:text-[var(--ph-text-0)]",
        danger:
          "border border-[var(--ph-red)]/40 bg-transparent text-[var(--ph-red)] hover:bg-[var(--ph-red)]/10",
      },
    },
    defaultVariants: { variant: "primary" },
  },
);

export interface PHButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof phButtonVariants> {
  href?: string;
}

export const PHButton = React.forwardRef<HTMLButtonElement, PHButtonProps>(
  ({ className, variant, href, type = "button", disabled, ...props }, ref) => {
    const classes = cn(phButtonVariants({ variant }), className);
    if (href) {
      // Button-only DOM attributes (onClick, aria-*, id, data-*, …) still
      // need to reach the rendered <a> — dropping `...props` here silently
      // discarded all of them for any href-using consumer. `disabled` has
      // no native meaning on an anchor, so it's translated to the
      // conventional aria-disabled + inert-click pattern instead.
      const linkProps = props as Omit<
        React.AnchorHTMLAttributes<HTMLAnchorElement>,
        "href"
      >;
      return (
        <Link
          {...linkProps}
          href={href}
          className={classes}
          aria-disabled={disabled}
          tabIndex={disabled ? -1 : undefined}
          onClick={disabled ? (e) => e.preventDefault() : linkProps.onClick}
        >
          {props.children}
        </Link>
      );
    }
    return (
      <button
        ref={ref}
        type={type}
        disabled={disabled}
        className={classes}
        {...props}
      />
    );
  },
);
PHButton.displayName = "PHButton";

/* ────────────────────────────────────────────────────────────────────
   PHChip
   ──────────────────────────────────────────────────────────────────── */

export function PHChip({
  active,
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean }) {
  return (
    <button
      type="button"
      aria-pressed={active}
      className={cn(
        "rounded-lg px-3 py-1.5 font-mono text-[10px] font-semibold uppercase tracking-[0.14em] transition-colors duration-150",
        active
          ? "bg-[var(--ph-blue-soft)] text-[var(--ph-blue)]"
          : "border border-[var(--ph-border)] text-[var(--ph-text-2)] hover:border-[var(--ph-border-strong)] hover:text-[var(--ph-text-0)]",
        className,
      )}
      {...props}
    />
  );
}

/* ────────────────────────────────────────────────────────────────────
   PHSurface / PHSectionLabel
   ──────────────────────────────────────────────────────────────────── */

export function PHSurface({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("ph-card rounded-2xl", className)} {...props} />;
}

export function PHSectionLabel({
  className,
  ...props
}: React.HTMLAttributes<HTMLSpanElement>) {
  return <span className={cn("ph-kicker", className)} {...props} />;
}

/* ────────────────────────────────────────────────────────────────────
   PHMetricCard
   ──────────────────────────────────────────────────────────────────── */

export type PHEmphasis = "default" | "blue" | "green" | "amber" | "red";

const emphasisText: Record<PHEmphasis, string> = {
  default: "text-[var(--ph-text-0)]",
  blue: "text-[var(--ph-blue)]",
  green: "text-[var(--ph-green)]",
  amber: "text-[var(--ph-amber)]",
  red: "text-[var(--ph-red)]",
};

export function PHMetricCard({
  label,
  value,
  subtext,
  emphasis = "default",
  className,
}: {
  label: string;
  value: React.ReactNode;
  subtext?: React.ReactNode;
  emphasis?: PHEmphasis;
  className?: string;
}) {
  return (
    <PHSurface className={cn("flex flex-col gap-2 p-5", className)}>
      <PHSectionLabel>{label}</PHSectionLabel>
      <span
        className={cn(
          "font-mono text-3xl font-semibold tracking-[-0.02em]",
          emphasisText[emphasis],
        )}
      >
        {value}
      </span>
      {subtext && (
        <span className="text-[13px] text-[var(--ph-text-2)]">
          {subtext}
        </span>
      )}
    </PHSurface>
  );
}

/* ────────────────────────────────────────────────────────────────────
   PHVerdictBadge
   ──────────────────────────────────────────────────────────────────── */

const verdictConfig: Record<
  PHVerdict,
  { label: string; color: string; bg: string; border: string }
> = {
  STRONG_HIRE: {
    label: "Strong hire",
    color: "var(--ph-green)",
    bg: "color-mix(in oklch, var(--ph-green) 14%, transparent)",
    border: "color-mix(in oklch, var(--ph-green) 45%, transparent)",
  },
  HIRE: {
    label: "Hire",
    color: "var(--ph-green)",
    bg: "color-mix(in oklch, var(--ph-green) 10%, transparent)",
    border: "color-mix(in oklch, var(--ph-green) 35%, transparent)",
  },
  MAYBE: {
    label: "Maybe",
    color: "var(--ph-amber)",
    bg: "color-mix(in oklch, var(--ph-amber) 12%, transparent)",
    border: "color-mix(in oklch, var(--ph-amber) 40%, transparent)",
  },
  NO_HIRE: {
    label: "No hire",
    color: "var(--ph-red)",
    bg: "color-mix(in oklch, var(--ph-red) 12%, transparent)",
    border: "color-mix(in oklch, var(--ph-red) 40%, transparent)",
  },
  INSUFFICIENT_DATA: {
    label: "Insufficient data",
    color: "var(--ph-blue)",
    bg: "var(--ph-blue-soft)",
    border: "color-mix(in oklch, var(--ph-blue) 40%, transparent)",
  },
};

export function PHVerdictBadge({
  verdict,
  className,
}: {
  verdict: PHVerdict;
  className?: string;
}) {
  const cfg = verdictConfig[verdict];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-xl border px-3 py-1.5 text-[12px] font-semibold",
        className,
      )}
      style={{ color: cfg.color, background: cfg.bg, borderColor: cfg.border }}
    >
      <PHDotIcon style={{ color: cfg.color }} />
      {cfg.label}
    </span>
  );
}

/* ────────────────────────────────────────────────────────────────────
   Score color thresholds shared by the gauge + bar.
   ──────────────────────────────────────────────────────────────────── */

function scoreColor(fraction: number): string {
  if (fraction >= 0.7) return "var(--ph-green)";
  if (fraction >= 0.4) return "var(--ph-amber)";
  return "var(--ph-red)";
}

/* ────────────────────────────────────────────────────────────────────
   PHScoreGauge
   ──────────────────────────────────────────────────────────────────── */

export function PHScoreGauge({
  score,
  max = 1,
  size = 120,
}: {
  score: number;
  max?: number;
  size?: number;
}) {
  const fraction = Math.max(0, Math.min(1, max === 0 ? 0 : score / max));
  const color = scoreColor(fraction);
  const strokeWidth = 8;
  const r = (size - strokeWidth) / 2;
  const c = 2 * Math.PI * r;
  const offset = c * (1 - fraction);

  return (
    <div
      className="relative inline-flex items-center justify-center"
      style={{ width: size, height: size }}
    >
      <svg
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        className="-rotate-90"
        style={{ filter: `drop-shadow(0 0 10px color-mix(in oklch, ${color} 45%, transparent))` }}
      >
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke="var(--ph-border)"
          strokeWidth={strokeWidth}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={strokeWidth}
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={offset}
          style={{ transition: "stroke-dashoffset 600ms ease" }}
        />
      </svg>
      <span
        className="absolute font-mono text-3xl font-semibold"
        style={{ color }}
      >
        {Math.round(fraction * 100)}
      </span>
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────────
   PHScoreBar
   ──────────────────────────────────────────────────────────────────── */

export function PHScoreBar({
  label,
  score,
  max = 1,
  className,
}: {
  label: string;
  score: number;
  max?: number;
  className?: string;
}) {
  const fraction = Math.max(0, Math.min(1, max === 0 ? 0 : score / max));
  const color = scoreColor(fraction);
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <div className="flex items-center justify-between">
        <span className="text-[13px] text-[var(--ph-text-1)]">{label}</span>
        <span className="font-mono text-[12px] text-[var(--ph-text-2)]">
          {Math.round(fraction * 100)}%
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-[var(--ph-surface-2)]">
        <div
          className="h-full rounded-full transition-[width] duration-500"
          style={{
            width: `${fraction * 100}%`,
            background: color,
            boxShadow: `0 0 12px color-mix(in oklch, ${color} 55%, transparent)`,
          }}
        />
      </div>
    </div>
  );
}

/* ────────────────────────────────────────────────────────────────────
   PHSeverityPip
   ──────────────────────────────────────────────────────────────────── */

export type PHSeverity = "high" | "medium" | "low";

const severityColor: Record<PHSeverity, string> = {
  high: "var(--ph-red)",
  medium: "var(--ph-amber)",
  low: "var(--ph-blue)",
};

export function PHSeverityPip({
  severity,
  className,
}: {
  severity: PHSeverity;
  className?: string;
}) {
  return (
    <span
      className={cn("inline-block h-2 w-2 shrink-0 rounded-full", className)}
      style={{
        background: severityColor[severity],
        boxShadow: `0 0 6px color-mix(in oklch, ${severityColor[severity]} 60%, transparent)`,
      }}
      aria-hidden
    />
  );
}
