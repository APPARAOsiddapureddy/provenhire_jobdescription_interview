"use client";

import React, { useEffect, useRef, useState } from "react";
import { AlertTriangle, Camera } from "lucide-react";
import { ModalScaffold } from "./interview-room-floor";
import { PHButton } from "@/components/design-system";

export function ProctoringViolationModal({
  message,
  onTimeout,
  onRetry,
}: {
  message: string;
  onTimeout: () => void;
  onRetry: () => void;
}) {
  const [secondsLeft, setSecondsLeft] = useState(20);
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);

  // Request camera for preview
  useEffect(() => {
    let cancelled = false;

    async function requestCamera() {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          video: { width: { ideal: 320 }, height: { ideal: 240 } },
        });
        if (!cancelled && videoRef.current) {
          streamRef.current = stream;
          videoRef.current.srcObject = stream;
          await videoRef.current.play().catch(() => {});
        }
      } catch (err) {
        // Camera access denied or unavailable - preview will show placeholder
        console.error("Camera access error:", err);
      }
    }

    requestCamera();

    return () => {
      cancelled = true;
      if (streamRef.current) {
        streamRef.current.getTracks().forEach((track) => track.stop());
        streamRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    if (secondsLeft <= 0) {
      onTimeout();
      return;
    }
    const t = setTimeout(() => setSecondsLeft((s) => s - 1), 1000);
    return () => clearTimeout(t);
  }, [secondsLeft, onTimeout]);

  return (
    <ModalScaffold titleId="ph-violation-title">
      <div className="flex justify-center mb-4">
        <div className="h-12 w-12 rounded-full bg-red-500/20 flex items-center justify-center">
          <AlertTriangle className="h-6 w-6 text-red-500" />
        </div>
      </div>
      <p
        id="ph-violation-title"
        className="text-[19px] font-semibold text-white text-center mb-2"
      >
        Camera Issue Detected
      </p>
      <p className="text-[13px] leading-relaxed text-white/60 text-center mb-6">
        {message}
      </p>

      {/* Camera Preview */}
      <div className="mb-6 rounded-lg overflow-hidden border-2 border-white/20 bg-black aspect-video flex items-center justify-center relative">
        <video
          ref={videoRef}
          autoPlay
          playsInline
          muted
          className="w-full h-full object-cover"
          style={{ display: streamRef.current ? "block" : "none" }}
        />
        {!streamRef.current && (
          <div className="flex flex-col items-center gap-2 text-center">
            <Camera className="h-8 w-8 text-white/40" />
            <p className="text-xs text-white/50">
              Camera preview will appear here
            </p>
          </div>
        )}
      </div>

      {/* Countdown */}
      <div className="mb-6 flex items-center justify-center gap-3">
        <div className="text-center">
          <div className="text-3xl font-mono font-bold text-red-500">
            {secondsLeft}s
          </div>
          <div className="text-xs text-white/50">Time remaining</div>
        </div>
      </div>

      {/* Progress bar */}
      <div className="mb-6 h-1 bg-white/10 rounded-full overflow-hidden">
        <div
          className="h-full bg-red-500 transition-all duration-1000"
          style={{ width: `${(secondsLeft / 20) * 100}%` }}
        />
      </div>

      {/* Actions */}
      <div className="space-y-3">
        <PHButton
          variant="primary"
          onClick={onRetry}
          className="w-full flex items-center justify-center gap-2"
        >
          <Camera className="h-4 w-4" />
          Turn Camera On
        </PHButton>
        <p className="text-[11px] text-white/50 text-center">
          Please enable your camera or remove any blocking objects from the
          camera view.
        </p>
      </div>
    </ModalScaffold>
  );
}
