/**
 * The server clock — the only clock the console is allowed to reason with
 * (spec C5, A12).
 *
 * There is exactly one time line here:
 *
 *     serverNow = anchorServerMs + (performance.now() - anchorPerfMs)
 *
 * `performance.now()` is monotonic, so a wrong local wall clock, a timezone
 * change or an NTP jump cannot move it. The anchor is refreshed by every HTTP
 * envelope and every console-socket event (see `lib/api.ts` and
 * `components/live-bridge.tsx`).
 *
 * There is deliberately **no `Date.now()` fallback**: before the first
 * calibration `serverNow()` returns `null` and callers must render a
 * "waiting for server time" state instead of inventing an answer.
 */

export interface ClockSnapshot {
  calibrated: boolean;
  /** Server clock value of the anchor. */
  anchorServerMs: number;
  /** `performance.now()` at the moment the anchor was taken. */
  anchorPerfMs: number;
}

const UNCALIBRATED: ClockSnapshot = Object.freeze({
  calibrated: false,
  anchorServerMs: 0,
  anchorPerfMs: 0,
});

let snapshot: ClockSnapshot = UNCALIBRATED;
const listeners = new Set<() => void>();

/** Stable snapshot for `useSyncExternalStore`. */
export function getClockSnapshot(): ClockSnapshot {
  return snapshot;
}

export function subscribeClock(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * Calibrate from any server timestamp. Ignores absent/nonsensical values so a
 * half-populated payload cannot poison the clock.
 */
export function syncClock(serverTimeMs: number | null | undefined): void {
  if (typeof serverTimeMs !== "number" || !Number.isFinite(serverTimeMs) || serverTimeMs <= 0) {
    return;
  }
  snapshot = {
    calibrated: true,
    anchorServerMs: serverTimeMs,
    anchorPerfMs: performance.now(),
  };
  for (const listener of listeners) listener();
}

export function isClockCalibrated(): boolean {
  return snapshot.calibrated;
}

/** Current server time in epoch ms, or `null` before the first calibration. */
export function serverNow(): number | null {
  if (!snapshot.calibrated) return null;
  return snapshot.anchorServerMs + Math.max(0, performance.now() - snapshot.anchorPerfMs);
}

/** Age of a server timestamp, in ms, or `null` when it cannot be known. */
export function serverAge(timestampMs: number | null | undefined): number | null {
  if (timestampMs === null || timestampMs === undefined || timestampMs <= 0) return null;
  const now = serverNow();
  if (now === null) return null;
  return Math.max(0, now - timestampMs);
}

/** Remaining ms until a server deadline, or `null` when it cannot be known. */
export function serverTimeLeft(deadlineMs: number | null | undefined): number | null {
  if (deadlineMs === null || deadlineMs === undefined || deadlineMs <= 0) return null;
  const now = serverNow();
  if (now === null) return null;
  return Math.max(0, deadlineMs - now);
}