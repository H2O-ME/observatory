/**
 * Live console-socket state.
 *
 * Time is *not* here: the one clock lives in `lib/clock.ts` and is calibrated by
 * `lib/api.ts` / `components/live-bridge.tsx` (spec C5). This store only carries
 * facts that come from the socket itself.
 */

import { create } from "zustand";

interface Channels {
  ws: number;
  poll: number;
}

interface LiveState {
  connected: boolean;
  version: string;
  channels: Channels;
  setConnected: (connected: boolean) => void;
  setMeta: (meta: { version?: string; channels?: Channels }) => void;
  reset: () => void;
}

const EMPTY_CHANNELS: Channels = { ws: 0, poll: 0 };

export const useLive = create<LiveState>((set) => ({
  connected: false,
  version: "",
  channels: EMPTY_CHANNELS,

  setConnected(connected) {
    set({ connected });
  },
  setMeta({ version, channels }) {
    set((state) => ({
      version: version ?? state.version,
      channels: channels ?? state.channels,
    }));
  },
  reset() {
    set({ connected: false, version: "", channels: EMPTY_CHANNELS });
  },
}));