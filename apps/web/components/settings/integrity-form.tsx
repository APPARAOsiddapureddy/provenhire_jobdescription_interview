"use client";

/**
 * <IntegrityForm> — the Integrity Controls settings editor.
 *
 * Client component: loads current settings from `/api/integrity-settings`
 * (falls back to all-OFF if unreachable), lets the operator apply a preset
 * or edit rows individually, saves via PUT, and can export the current
 * (possibly unsaved) form state as CSV. No auth gate — see the route's own
 * comment for why that's an accepted trade-off in this OSS build.
 */

import * as React from "react";
import {
  DEFAULT_INTEGRITY_SETTINGS,
  INTEGRITY_PRESETS,
  IntegritySettingsSchema,
  type IntegrityRuleState,
  type IntegritySettings,
} from "@proven-hire/shared";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { ToggleRow } from "@/components/ui/toggle-row";
import { downloadCsv, toCsv } from "@/lib/csv";

const RULES: {
  key: keyof Omit<IntegritySettings, "updated_at">;
  label: string;
  description: string;
}[] = [
  {
    key: "ai_behavior_analysis",
    label: "AI Behavior Analysis",
    description: "AI-driven behavior analysis during assessment",
  },
  {
    key: "camera_required",
    label: "Camera Required",
    description: "Require webcam to be on during assessment",
  },
  {
    key: "copy_paste_detection",
    label: "Copy Paste Detection",
    description: "Block copy-paste and context menu during assessment",
  },
  {
    key: "devtools_detection",
    label: "Devtools Detection",
    description: "Detect when developer tools are opened during assessment",
  },
  {
    key: "fullscreen_required",
    label: "Fullscreen Required",
    description: "Require fullscreen mode during assessment",
  },
  {
    key: "microphone_monitoring",
    label: "Microphone Monitoring (background voice, etc.)",
    description: "Monitor microphone for unauthorized audio",
  },
  {
    key: "camera_ai_detection",
    label: "Camera AI: multi-face, phone, no-face, low light",
    description:
      "Detect multiple faces, phones, absence, and low light in camera view",
  },
  {
    key: "three_strike_auto_end",
    label: "3-strike auto-end (strict proctoring)",
    description:
      "Auto-end assessment after 3 repeated alerts (no face, faces, phone, tab, fullscreen, voice noise). Use OFF for labs.",
  },
  {
    key: "screen_recording_enabled",
    label: "Screen Recording Enabled",
    description: "Enable screen recording during assessment",
  },
  {
    key: "tab_switching_detection",
    label: "Tab Switching Detection",
    description: "Detect if candidate switches browser tab during assessment",
  },
];

type SaveState = "idle" | "saving" | "saved" | "error";

export function IntegrityForm() {
  const [settings, setSettings] = React.useState<IntegritySettings>(
    DEFAULT_INTEGRITY_SETTINGS,
  );
  const [loading, setLoading] = React.useState(true);
  const [saveState, setSaveState] = React.useState<SaveState>("idle");

  React.useEffect(() => {
    let cancelled = false;
    fetch("/api/integrity-settings")
      .then((r) => r.json())
      .then((json) => {
        if (cancelled) return;
        const parsed = IntegritySettingsSchema.safeParse(json);
        if (parsed.success) setSettings(parsed.data);
      })
      .catch(() => {
        // Stay on defaults — the route itself already falls back safely,
        // this only covers a network failure reaching our own API route.
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function setRule(key: keyof IntegritySettings, value: IntegrityRuleState) {
    setSettings((prev) => ({ ...prev, [key]: value }));
    setSaveState("idle");
  }

  function applyPreset(preset: keyof typeof INTEGRITY_PRESETS) {
    setSettings((prev) => ({
      ...INTEGRITY_PRESETS[preset],
      updated_at: prev.updated_at,
    }));
    setSaveState("idle");
  }

  async function save() {
    setSaveState("saving");
    try {
      const res = await fetch("/api/integrity-settings", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(settings),
      });
      if (!res.ok) throw new Error("save failed");
      const json = await res.json();
      const parsed = IntegritySettingsSchema.safeParse(json);
      if (parsed.success) setSettings(parsed.data);
      setSaveState("saved");
    } catch {
      setSaveState("error");
    }
  }

  function exportCsv() {
    const rows = RULES.map((rule) => ({
      rule: rule.label,
      key: rule.key,
      state: settings[rule.key] as string,
    }));
    downloadCsv("integrity-settings.csv", toCsv(rows));
  }

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div>
          <CardTitle>Integrity Controls</CardTitle>
          <p className="mt-1 text-sm text-muted">
            Configure proctoring and integrity monitoring. Changes apply
            globally without redeploy.
          </p>
        </div>
        <Button variant="out" size="sm" onClick={exportCsv} disabled={loading}>
          CSV
        </Button>
      </CardHeader>
      <CardContent>
        <p className="mb-4 text-[12.5px] leading-relaxed text-faint">
          OFF = disabled · MONITOR = log + learner warnings (no auto-end from
          strikes) · STRICT = full enforcement. 3-strike auto-end applies only
          when &ldquo;3-strike auto-end&rdquo; is STRICT — it ends the run after
          three repeated alerts for the same rule class (no face, extra faces,
          phone, tab leave, fullscreen exit, loud / multi-voice audio).
          Copy-paste does not use strike toasts.
        </p>

        <div className="mb-5 flex flex-wrap gap-2">
          <Button
            variant="out"
            size="sm"
            onClick={() => applyPreset("computer_lab")}
            disabled={loading}
          >
            Preset: Computer lab
          </Button>
          <Button
            variant="out"
            size="sm"
            onClick={() => applyPreset("remote_exam_strict")}
            disabled={loading}
          >
            Preset: Remote exam (strict)
          </Button>
        </div>

        <div className={loading ? "opacity-50" : undefined}>
          {RULES.map((rule) => (
            <ToggleRow
              key={rule.key}
              label={rule.label}
              description={rule.description}
              value={settings[rule.key] as IntegrityRuleState}
              onChange={(v) => setRule(rule.key, v)}
            />
          ))}
        </div>

        <div className="mt-6 flex items-center gap-3">
          <Button onClick={save} disabled={loading || saveState === "saving"}>
            {saveState === "saving" ? "Saving…" : "Save changes"}
          </Button>
          {saveState === "saved" && (
            <span className="text-[13px] text-ok">Saved.</span>
          )}
          {saveState === "error" && (
            <span className="text-[13px] text-[#b42318]">
              Could not save — the settings service may be unreachable.
            </span>
          )}
        </div>

        <p className="mt-6 border-t border-line pt-4 text-[12px] leading-relaxed text-faint">
          UX: Even when proctoring is OFF, the platform displays:
          &ldquo;ProvenHire uses integrity monitoring to ensure fair
          assessments.&rdquo; This maintains recruiter trust.
        </p>
      </CardContent>
    </Card>
  );
}
