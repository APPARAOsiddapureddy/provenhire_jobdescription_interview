"use client";

import React, { useEffect, useState, useRef } from "react";
import { AlertTriangle, Camera, CheckCircle2 } from "lucide-react";
import { PHButton } from "@/components/design-system";
import { useCameraMonitor } from "@/lib/integrity/useCameraMonitor";
import { useIntegrityMonitor } from "@/lib/integrity/useIntegrityMonitor";

export function ProctoringCheck({
  sessionId,
  onPassed,
}: {
  sessionId: string;
  onPassed: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [allChecksPassed, setAllChecksPassed] = useState(false);
  const [cameraChecksPassed, setCameraChecksPassed] = useState(false);

  // Use integrity monitor to get settings, camera status, and face detection
  const { settings, cameraStatus, faceDetected } = useIntegrityMonitor(
    sessionId,
    () => {},
    null,
  );

  const cameraReady = cameraStatus === "ok";
  const cameraRequested = cameraStatus === "requesting";
  const cameraDenied = cameraStatus === "denied";

  // Request camera and display preview
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
          // Ensure video plays
          videoRef.current.play().catch(() => {});
        }
      } catch (err) {
        // Camera access denied or unavailable
        console.error("Camera access error:", err);
      }
    }

    // Always try to request camera when component mounts
    if (!streamRef.current) {
      requestCamera();
    }

    return () => {
      cancelled = true;
      if (streamRef.current) {
        streamRef.current.getTracks().forEach((track) => track.stop());
        streamRef.current = null;
      }
    };
  }, []);

  // Check if camera is available and face is detected
  useEffect(() => {
    if (
      faceDetected &&
      settings?.camera_required !== "off" &&
      settings?.camera_ai_detection !== "off"
    ) {
      setCameraChecksPassed(true);
    } else {
      setCameraChecksPassed(false);
    }
  }, [faceDetected, settings]);

  const handleStartInterview = () => {
    if (cameraChecksPassed) {
      onPassed();
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#020304]">
      <div className="w-full max-w-md rounded-2xl border border-white/10 bg-gradient-to-br from-white/5 to-white/[0.02] p-8 backdrop-blur-xl">
        <div className="mb-6 flex justify-center">
          <div className="rounded-full bg-blue-500/10 p-4">
            <Camera className="h-8 w-8 text-blue-400" />
          </div>
        </div>

        <h1 className="text-center text-2xl font-semibold text-white mb-2">
          Camera Check
        </h1>
        <p className="text-center text-sm text-white/60 mb-8">
          We need to verify your camera is working before we begin.
        </p>

        {/* Camera Status */}
        <div className="space-y-3 mb-6">
          <div className="flex items-center gap-3 rounded-lg border border-white/10 bg-white/5 p-4">
            <div
              className={`h-2 w-2 rounded-full ${cameraReady ? "bg-green-500" : cameraDenied ? "bg-red-500" : "bg-yellow-500 animate-pulse"}`}
            />
            <div className="flex-1">
              <div className="text-sm font-medium text-white">
                {cameraReady
                  ? "Camera Access Granted"
                  : cameraDenied
                    ? "Camera Access Denied"
                    : cameraRequested
                      ? "Requesting Camera Access..."
                      : "Camera Not Detected"}
              </div>
              <div className="text-xs text-white/50">
                {cameraReady
                  ? "Your camera is ready to use"
                  : cameraDenied
                    ? "Please enable camera in your browser settings"
                    : cameraRequested
                      ? "Please allow camera access in the popup"
                      : "Click the button below to enable camera"}
              </div>
            </div>
            {cameraReady && <CheckCircle2 className="h-5 w-5 text-green-500" />}
            {cameraDenied && <AlertTriangle className="h-5 w-5 text-red-500" />}
          </div>

          <div className="flex items-center gap-3 rounded-lg border border-white/10 bg-white/5 p-4">
            <div
              className={`h-2 w-2 rounded-full ${faceDetected ? "bg-green-500" : cameraReady ? "bg-yellow-500 animate-pulse" : "bg-gray-500"}`}
            />
            <div className="flex-1">
              <div className="text-sm font-medium text-white">
                {faceDetected
                  ? "Face Detected ✓"
                  : cameraReady
                    ? "Detecting face..."
                    : "Waiting for camera"}
              </div>
              <div className="text-xs text-white/50">
                {faceDetected
                  ? "Your face is visible and you're ready to start"
                  : cameraReady
                    ? "Position yourself in the camera view (make sure your face is visible and centered)"
                    : "Enable camera first"}
              </div>
            </div>
            {faceDetected && (
              <CheckCircle2 className="h-5 w-5 text-green-500" />
            )}
          </div>
        </div>

        {/* Connection Status */}
        {cameraRequested && (
          <div className="mb-6 rounded-lg bg-blue-500/10 border border-blue-500/30 p-4 text-center">
            <div className="animate-spin h-6 w-6 border-2 border-blue-500 border-t-transparent rounded-full mx-auto mb-2" />
            <p className="text-sm text-blue-300">
              Connecting to your camera...
            </p>
          </div>
        )}

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

        {/* Actions */}
        <div className="space-y-3">
          {cameraDenied && (
            <p className="text-xs text-red-400 text-center">
              Camera access was denied. Please check your browser permissions
              and try again.
            </p>
          )}

          <PHButton
            variant="primary"
            onClick={handleStartInterview}
            disabled={!cameraChecksPassed}
            className="w-full"
          >
            {cameraChecksPassed ? "Start Interview" : "Waiting for Camera..."}
          </PHButton>

          <p className="text-xs text-white/50 text-center">
            This interview requires your camera to be on for the entire session.
          </p>
        </div>
      </div>
    </div>
  );
}
