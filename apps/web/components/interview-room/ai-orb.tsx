"use client";

/**
 * <AIOrb> — the AI interviewer's only "body language." Pure CSS radial-
 * gradient sphere (no WebGL dependency) whose color, pulse speed, and
 * amplitude change by state:
 *   idle       cool gray-blue, no glow, slow drift
 *   listening  white-hot center → cyan → blue → violet, pulsing cyan ring,
 *              amplitude reacts to `micEnergy`
 *   thinking   amber → violet, rotating conic scan ring, 3-dot bounce
 *   speaking   blue-white → violet → pink, expanding ring pulses, fastest
 */

import * as React from "react";
import { cn } from "@/lib/cn";

export type OrbState = "idle" | "listening" | "thinking" | "speaking";

const ORB_GRADIENT: Record<OrbState, string> = {
  idle: "radial-gradient(circle at 35% 30%, oklch(0.55 0.03 250), oklch(0.24 0.02 255) 65%, oklch(0.14 0.02 260) 100%)",
  listening:
    "radial-gradient(circle at 35% 30%, oklch(0.97 0.02 210), oklch(0.75 0.15 210) 30%, oklch(0.62 0.19 255) 62%, oklch(0.55 0.2 292) 100%)",
  thinking:
    "radial-gradient(circle at 35% 30%, oklch(0.86 0.14 80), oklch(0.7 0.17 60) 40%, oklch(0.55 0.2 292) 100%)",
  speaking:
    "radial-gradient(circle at 35% 30%, oklch(0.95 0.05 250), oklch(0.68 0.19 255) 34%, oklch(0.62 0.22 292) 68%, oklch(0.68 0.2 340) 100%)",
};

const RING_COLOR: Record<OrbState, string> = {
  idle: "oklch(0.4 0.02 255 / 0.3)",
  listening: "oklch(0.78 0.16 210 / 0.65)",
  thinking: "oklch(0.8 0.16 72 / 0.6)",
  speaking: "oklch(0.7 0.19 255 / 0.7)",
};

export function AIOrb({
  state,
  micEnergy = 0,
  size = 220,
  className,
}: {
  state: OrbState;
  micEnergy?: number;
  size?: number;
  className?: string;
}) {
  const amplitude = state === "listening" ? 1 + micEnergy * 0.25 : 1;
  const pulseClass =
    state === "speaking" || state === "thinking"
      ? "ph-anim-pulse-fast"
      : state === "listening"
        ? "ph-anim-pulse"
        : "";

  return (
    <div
      className={cn("relative flex items-center justify-center", className)}
      style={{ width: size, height: size }}
    >
      {/* Expanding ring pulses — speaking only. */}
      {state === "speaking" && (
        <>
          <span
            className="ph-anim-ring absolute rounded-full border"
            style={{
              width: size * 0.7,
              height: size * 0.7,
              borderColor: RING_COLOR[state],
            }}
          />
          <span
            className="ph-anim-ring absolute rounded-full border"
            style={{
              width: size * 0.7,
              height: size * 0.7,
              borderColor: RING_COLOR[state],
              animationDelay: "0.6s",
            }}
          />
        </>
      )}

      {/* Rotating conic scan ring — thinking only. */}
      {state === "thinking" && (
        <span
          className="ph-anim-conic absolute rounded-full"
          style={{
            width: size * 0.86,
            height: size * 0.86,
            background: `conic-gradient(from 0deg, transparent 0%, ${RING_COLOR.thinking} 12%, transparent 24%)`,
            mask: "radial-gradient(farthest-side, transparent calc(100% - 3px), black calc(100% - 2px))",
            WebkitMask:
              "radial-gradient(farthest-side, transparent calc(100% - 3px), black calc(100% - 2px))",
          }}
        />
      )}

      {/* Pulsing outline ring — listening. */}
      {state === "listening" && (
        <span
          className="ph-anim-pulse absolute rounded-full border-2"
          style={{
            width: size * 0.82,
            height: size * 0.82,
            borderColor: RING_COLOR.listening,
          }}
        />
      )}

      {/* The sphere itself. */}
      <span
        className={cn("rounded-full", pulseClass)}
        style={{
          width: size * 0.62,
          height: size * 0.62,
          background: ORB_GRADIENT[state],
          boxShadow: `0 0 ${40 * amplitude}px color-mix(in oklch, ${RING_COLOR[state]} 70%, transparent), inset 0 0 30px oklch(0 0 0 / 0.25)`,
          transform: `scale(${amplitude})`,
          transition: "transform 120ms ease-out, background 400ms ease",
        }}
      />

      {/* 3-dot thinking bounce, overlaid at the base. */}
      {state === "thinking" && (
        <div className="absolute bottom-[18%] flex items-center gap-1.5">
          {[0, 1, 2].map((i) => (
            <span
              key={i}
              className="ph-anim-dot h-1.5 w-1.5 rounded-full bg-[var(--ph-text-0)]"
              style={{ animationDelay: `${i * 200}ms` }}
            />
          ))}
        </div>
      )}
    </div>
  );
}
