import { useEffect, useState } from "react";

import { serverNow } from "@/store/live";

/**
 * A tick that follows the server clock.
 *
 * Re-renders roughly once per second so "12 秒前" ages correctly even when the
 * browser tab was suspended for a while; the underlying value comes from the
 * server-synced clock, not `Date.now()`.
 */
export function useServerNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => serverNow());

  useEffect(() => {
    setNow(serverNow());
    const timer = window.setInterval(() => setNow(serverNow()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);

  return now;
}

/** Age in milliseconds of a server timestamp, against the server clock. */
export function useAge(timestampMs: number | null | undefined): number | null {
  const now = useServerNow();
  if (!timestampMs) return null;
  return Math.max(0, now - timestampMs);
}