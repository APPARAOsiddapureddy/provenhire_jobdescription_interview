"use client";

/**
 * <ToggleRow> — a labeled OFF / MONITOR / STRICT segmented control.
 *
 * Purpose-built for the Integrity Controls settings page: each proctoring
 * rule is one row (label + description + this 3-way switch). Not a generic
 * shadcn `Select` — with exactly three fixed states, a segmented control
 * reads faster than a dropdown and matches this app's `Badge`/`Button`
 * visual language directly (no new primitive dependency needed).
 */

import * as React from "react";
import { cn } from "@/lib/cn";
import type { IntegrityRuleState } from "@proven-hire/shared";

const STATES: IntegrityRuleState[] = ["off", "monitor", "strict"];

const STATE_LABEL: Record<IntegrityRuleState, string> = {
  off: "OFF",
  monitor: "MONITOR",
  strict: "STRICT",
};

const STATE_ACTIVE_CLASS: Record<IntegrityRuleState, string> = {
  off: "bg-ink-soft text-white",
  monitor: "bg-accent text-white",
  strict: "bg-[#b42318] text-white",
};

export interface ToggleRowProps {
  label: string;
  description: string;
  value: IntegrityRuleState;
  onChange: (value: IntegrityRuleState) => void;
  className?: string;
}

export function ToggleRow({
  label,
  description,
  value,
  onChange,
  className,
}: ToggleRowProps) {
  // ARIA APG roving-tabindex pattern for role="radio" groups: only the
  // selected segment is Tab-stoppable; arrow keys move focus AND selection
  // together between segments (OS-style segmented-control behavior), with
  // focus programmatically following the new selection.
  const buttonRefs = React.useRef<
    Partial<Record<IntegrityRuleState, HTMLButtonElement | null>>
  >({});

  function selectByOffset(fromIndex: number, offset: number) {
    const nextIndex = (fromIndex + offset + STATES.length) % STATES.length;
    const next = STATES[nextIndex]!;
    onChange(next);
    buttonRefs.current[next]?.focus();
  }

  function handleKeyDown(
    event: React.KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) {
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      event.preventDefault();
      selectByOffset(index, 1);
    } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      event.preventDefault();
      selectByOffset(index, -1);
    }
  }

  return (
    <div
      className={cn(
        "flex flex-col gap-3 border-b border-line py-4 last:border-b-0",
        "sm:flex-row sm:items-center sm:justify-between",
        className,
      )}
    >
      <div className="max-w-md">
        <p className="text-[14px] font-medium text-ink">{label}</p>
        <p className="mt-0.5 text-[12.5px] leading-relaxed text-muted">
          {description}
        </p>
      </div>
      <div
        role="radiogroup"
        aria-label={label}
        className="inline-flex shrink-0 rounded-[10px] border border-line bg-panel p-0.5"
      >
        {STATES.map((state, index) => (
          <button
            key={state}
            ref={(el) => {
              buttonRefs.current[state] = el;
            }}
            type="button"
            role="radio"
            aria-checked={value === state}
            tabIndex={value === state ? 0 : -1}
            onClick={() => onChange(state)}
            onKeyDown={(event) => handleKeyDown(event, index)}
            className={cn(
              "rounded-[8px] px-3 py-1.5 text-[11px] font-mono font-medium tracking-wide",
              "transition-colors duration-150 cursor-pointer",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-1 focus-visible:ring-offset-paper",
              value === state
                ? STATE_ACTIVE_CLASS[state]
                : "text-ink-soft hover:bg-accent-soft",
            )}
          >
            {STATE_LABEL[state]}
          </button>
        ))}
      </div>
    </div>
  );
}
