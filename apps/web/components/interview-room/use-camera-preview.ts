"use client";

/**
 * A small, self-contained camera preview — mirrors <DeviceCheck>'s
 * getUserMedia/teardown pattern. Never published anywhere; purely a local
 * preview for the candidate corner (see spec: "mirrored camera preview,
 * falls back to a circular initials avatar when camera off").
 */

import * as React from "react";

export function useCameraPreview() {
  const [cameraOn, setCameraOn] = React.useState(false);
  const [stream, setStream] = React.useState<MediaStream | null>(null);
  const streamRef = React.useRef<MediaStream | null>(null);
  const hasTriedToEnableRef = React.useRef(false);

  // Auto-enable camera preview when interview starts
  React.useEffect(() => {
    if (hasTriedToEnableRef.current) return;
    hasTriedToEnableRef.current = true;

    async function enableCamera() {
      if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
        return;
      }
      try {
        const s = await navigator.mediaDevices.getUserMedia({
          video: { width: { ideal: 320 }, height: { ideal: 240 } }
        });
        streamRef.current = s;
        setStream(s);
        setCameraOn(true);
      } catch (err) {
        console.error("Failed to enable camera preview:", err);
        setCameraOn(false);
      }
    }

    enableCamera();
  }, []);

  React.useEffect(
    () => () => {
      streamRef.current?.getTracks().forEach((t) => t.stop());
    },
    [],
  );

  const toggleCamera = React.useCallback(async () => {
    if (cameraOn) {
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      setStream(null);
      setCameraOn(false);
      return;
    }
    if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
      return;
    }
    try {
      const s = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 320 }, height: { ideal: 240 } }
      });
      streamRef.current = s;
      setStream(s);
      setCameraOn(true);
    } catch (err) {
      console.error("Failed to enable camera:", err);
      setCameraOn(false);
    }
  }, [cameraOn]);

  return { cameraOn, cameraStream: stream, toggleCamera };
}
