/**
 * The console-facing clock surface (spec §4.3 `store/clock`).
 *
 * The engine is `lib/clock.ts`: one anchor, no `Date.now()` fallback. This module
 * exists so React code has a single import path for both the value helpers and
 * the hooks, while non-React code (`lib/api.ts`) talks to the engine directly.
 * It holds no state of its own — a second clock would be a second truth.
 */

export {
  getClockSnapshot,
  isClockCalibrated,
  serverAge,
  serverNow,
  serverTimeLeft,
  subscribeClock,
  syncClock,
} from "@/lib/clock";
export type { ClockSnapshot } from "@/lib/clock";
export { useServerAge, useServerNow } from "@/hooks/use-server-now";