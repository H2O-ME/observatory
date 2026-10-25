/**
 * Bridges the server event socket into the clock, the live store and React Query.
 *
 * The socket is the second calibration source for the server clock (the first is
 * every HTTP envelope). Every event carries a server timestamp, so the console's
 * clock is re-anchored continuously while the connection is up — a tab that was
 * suspended for ten minutes is correct again on the next tick (spec C5).
 */

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { openEventSocket } from "@/lib/api";
import type { SocketEvent } from "@/lib/api";
import { syncClock } from "@/store/clock";
import { useAuth } from "@/store/auth";
import { useLive } from "@/store/live";

function channelsOf(data: Record<string, unknown> | undefined): { ws: number; poll: number } | null {
  const channels = data?.channels;
  if (!channels || typeof channels !== "object") return null;
  const record = channels as { ws?: unknown; poll?: unknown };
  return { ws: Number(record.ws ?? 0), poll: Number(record.poll ?? 0) };
}

/** frame_added burst coalescer — see the handler below. Module-level so a
 *  socket reconnect never doubles the flush timer. */
const pendingFrameInvalidation: { timer: number | undefined; uids: Set<string> } = {
  timer: undefined,
  uids: new Set(),
};

export function LiveBridge() {
  const queryClient = useQueryClient();

  useEffect(() => {
    const { setConnected, setMeta } = useLive.getState();

    let socket: { setWatchdogMs: (ms: number) => void } | null = null;

    const handle = (event: SocketEvent) => {
      // Every event carries the server's clock: recalibrate before anything else.
      if (typeof event.ts_ms === "number") syncClock(event.ts_ms);

      switch (event.type) {
        case "socket_open":
          setConnected(true);
          void queryClient.invalidateQueries({ queryKey: ["devices"] });
          return;
        case "socket_closed":
          setConnected(false);
          return;
        case "hello": {
          const version = event.data?.version;
          if (typeof version === "string") setMeta({ version });
          const channels = channelsOf(event.data);
          if (channels) setMeta({ channels });
          const heartbeat = Number(event.data?.heartbeat_s ?? 0);
          if (heartbeat > 0) socket?.setWatchdogMs(heartbeat * 1000 * 2.5 + 5_000);
          return;
        }
        case "tick": {
          const channels = channelsOf(event.data);
          if (channels) setMeta({ channels });
          return;
        }
        default:
          break;
      }

      const uid = event.device_uid;
      if (uid) {
        void queryClient.invalidateQueries({ queryKey: ["device", uid] });
        // 0.3.1 F4: per-device effective settings have no refetchInterval of
        // their own — without this invalidation two operators never see each
        // other's device overrides.
        void queryClient.invalidateQueries({ queryKey: ["device-settings", uid] });
      }
      if (event.type === "device_updated" || event.type === "device_removed" || event.type === "uninstall_progress") {
        void queryClient.invalidateQueries({ queryKey: ["devices"] });
      }
      if (event.type === "frame_added") {
        // The wall renders `latest_frame_id` from the device list, so a new frame
        // also invalidates the list (the 0.2.x bridge forgot this and the wall
        // waited for its poll). The timeline drill-down and the archive counters
        // move with it.
        //
        // Burst coalescing (0.3.7): a fleet capturing on its own interval can
        // emit dozens of frame_added per second; invalidating five query trees
        // per event hammered the API and re-rendered every list on the watch.
        // Collect for 1.5s, then invalidate once.
        pendingFrameInvalidation.uids.add(uid ?? "");
        if (!pendingFrameInvalidation.timer) {
          pendingFrameInvalidation.timer = window.setTimeout(() => {
            pendingFrameInvalidation.timer = undefined;
            pendingFrameInvalidation.uids.clear();
            void queryClient.invalidateQueries({ queryKey: ["devices"] });
            void queryClient.invalidateQueries({ queryKey: ["frames"] });
            void queryClient.invalidateQueries({ queryKey: ["timeline"] });
            void queryClient.invalidateQueries({ queryKey: ["backup-overview"] });
            void queryClient.invalidateQueries({ queryKey: ["backup-entries"] });
          }, 1_500);
        }
      }
      if (event.type === "device_removed") {
        void queryClient.invalidateQueries({ queryKey: ["backup-overview"] });
        void queryClient.invalidateQueries({ queryKey: ["backup-entries"] });
      }
      if (event.type === "command_created" || event.type === "command_updated") {
        void queryClient.invalidateQueries({ queryKey: ["commands"] });
      }
      if (event.type === "notice" || event.type === "version") {
        void queryClient.invalidateQueries({ queryKey: ["updates"] });
        void queryClient.invalidateQueries({ queryKey: ["system"] });
        void queryClient.invalidateQueries({ queryKey: ["settings"] });
        void queryClient.invalidateQueries({ queryKey: ["install-tokens"] });
      }
    };

    const opened = openEventSocket({
      onEvent: handle,
      onAuthFailure: () => {
        // Session expired while the socket was open: asking the server for a
        // session turns this into an anonymous state and shows the login page.
        void useAuth.getState().refresh();
      },
    });
    socket = opened;

    return () => opened.close();
  }, [queryClient]);

  return null;
}