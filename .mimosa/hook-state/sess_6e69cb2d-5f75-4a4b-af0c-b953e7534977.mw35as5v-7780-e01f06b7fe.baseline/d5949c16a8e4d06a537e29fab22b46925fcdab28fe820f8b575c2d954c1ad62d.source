/**
 * Server-clock React hooks.
 *
 * Two properties matter here:
 *  - The value comes from `lib/clock.ts`, never from `Date.now()`. Before the
 *    first calibration the hooks return `null` and the view must say so.
 *  - There is **one** 1 Hz ticker for the whole application (module level,
 *    reference-counted by subscribers). The 0.2.x console started one interval
 *    per device card, which turned a wall of 40 devices into 80 timers.
 */

import { useSyncExternalStore } from "react";

import { getClockSnapshot, serverNow, subscribeClock } from "@/lib/clock";

let tick = 0;
const tickListeners = new Set<() => void>();
let tickerId: number | undefined;

function onTick(): void {
  tick += 1;
  for (const listener of tickListeners) listener();
}

export function subscribeSecondTick(listener: () => void): () => void {
  tickListeners.add(listener);
  if (tickerId === undefined) {
    tickerId = window.setInterval(onTick, 1000);
  }
  return () => {
    tickListeners.delete(listener);
    if (tickListeners.size === 0 && tickerId !== undefined) {
      window.clearInterval(tickerId);
      tickerId = undefined;
    }
  };
}

export function getSecondTick(): number {
  return tick;
}

/**
 * Current server time in epoch ms, re-rendered about once per second.
 * Returns `null` until a response or socket event has calibrated the clock.
 */
export function useServerNow(): number | null {
  const clock = useSyncExternalStore(subscribeClock, getClockSnapshot, getClockSnapshot);
  // Subscribing also schedules the re-render; the returned tick is not needed.
  useSyncExternalStore(subscribeSecondTick, getSecondTick, getSecondTick);
  return clock.calibrated ? serverNow() : null;
}

/** Age of a server timestamp against the server clock. `null` when unknown. */
export function useServerAge(timestampMs: number | null | undefined): number | null {
  const now = useServerNow();
  if (now === null || timestampMs === null || timestampMs === undefined || timestampMs <= 0) {
    return null;
  }
  return Math.max(0, now - timestampMs);
}