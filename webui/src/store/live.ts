/**
 * Live connection state and the *server* clock.
 *
 * The console must never decide whether a device is online from its own clock: a
 * laptop with a wrong timezone or a sleeping tab then disagrees with the server and
 * paints the device grey while data is still arriving. Everything that needs "now"
 * goes through `serverNow()`, which is the server's reported time plus the elapsed
 * monotonic duration since we received it.
 */

import { create } from "zustand";

interface LiveState {
  connected: boolean;
  /** server_time_ms at the moment we last synced. */
  syncedServerMs: number;
  /** performance.now() when we synced. */
  syncedAt: number;
  eventSeq: number;
  lastEventType: string;
  version: string;
  channels: { ws: number; poll: number };
  setConnected: (connected: boolean) => void;
  syncTime: (serverMs: number) => void;
  pushEvent: (type: string) => void;
  setMeta: (meta: { version?: string; channels?: { ws: number; poll: number } }) => void;
}

export const useLive = create<LiveState>((set) => ({
  connected: false,
  syncedServerMs: 0,
  syncedAt: 0,
  eventSeq: 0,
  lastEventType: "",
  version: "",
  channels: { ws: 0, poll: 0 },

  setConnected(connected) {
    set({ connected });
  },
  syncTime(serverMs) {
    if (!serverMs) return;
    set({ syncedServerMs: serverMs, syncedAt: performance.now() });
  },
  pushEvent(type) {
    set((state) => ({ eventSeq: state.eventSeq + 1, lastEventType: type }));
  },
  setMeta({ version, channels }) {
    set((state) => ({
      version: version ?? state.version,
      channels: channels ?? state.channels,
    }));
  },
}));

/**
 * Current server time in epoch milliseconds.
 *
 * Falls back to `Date.now()` only when we have never received a server timestamp,
 * and even then the fallback is flagged by `useLive().connected === false` at the
 * call sites that care.
 */
export function serverNow(): number {
  const { syncedServerMs, syncedAt } = useLive.getState();
  if (!syncedServerMs) return Date.now();
  if (!syncedAt) return syncedServerMs;
  return syncedServerMs + Math.max(0, performance.now() - syncedAt);
}

/** Clock offset between this browser and the server, in milliseconds. */
export function clockOffset(): number {
  return serverNow() - Date.now();
}