/**
 * components/Toast.tsx — transient success/error notice (KER-303 AC-5).
 *
 * What:  a fixed-position message that disappears after a few seconds.
 * Why:   review actions need visible confirmation without interrupting flow.
 * How:   rendered by RecommendationList with a key so re-triggers restart the
 *        timer. Tests: npm test.
 */

"use client";

import { useEffect, useState } from "react";

const TOAST_VISIBLE_MILLISECONDS = 4000;

interface ToastProps {
  message: string;
  tone: "success" | "error";
}

export default function Toast({ message, tone }: ToastProps) {
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    const timer = setTimeout(() => setVisible(false), TOAST_VISIBLE_MILLISECONDS);
    return () => clearTimeout(timer);
  }, [message, tone]);

  if (!visible) {
    return null;
  }
  return (
    <div
      role="status"
      className={`fixed bottom-6 right-6 rounded-lg px-4 py-3 text-sm shadow-lg ${
        tone === "success"
          ? "bg-green-100 text-green-900 border border-green-300"
          : "bg-red-100 text-red-900 border border-red-300"
      }`}
    >
      {message}
    </div>
  );
}
