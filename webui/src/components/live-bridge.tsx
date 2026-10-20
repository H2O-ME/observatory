import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { openEventSocket } from "@/lib/api";
import type { SocketEvent } from "@/lib/api";
import { useLive } from "@/store/live";

/**
 * Bridges the server event socket into React Query.
 *
 * Two jobs: keep `useLive` (connection + server clock) current, and invalidate the
 * queries an event actually affects. Invalidation is intentional rather than
 * clever — the server is the only source of truth for device state.
 */
export function LiveBridge() {
  const queryClient = useQueryClient();
  const { setConnected, syncTime, pushEvent, setMeta } = useLive();

  useEffect(() => {
    const handle = (event: SocketEvent) => {
      const type = event.type;
      if (type === "socket_open") {
        setConnected(true);
        void queryClient.invalidateQueries({ queryKey: ["devices"] });
        return;
      }
      if (type === "socket_closed") {
        setConnected(false);
        return;
      }

      const serverTime = Number((event.data?.server_time_ms as number | undefined) ?? event.ts_ms ?? 0);
      if (serverTime) syncTime(serverTime);
      pushEvent(type);

      if (type === "hello") {
        const version = event.data?.version;
        if (typeof version === "string") setMeta({ version });
        const channels = event.data?.channels;
        if (channels && typeof channels === "object") {
          const record = channels as { ws?: number; poll?: number };
          setMeta({ channels: { ws: Number(record.ws ?? 0), poll: Number(record.poll ?? 0) } });
        }
        return;
      }
      if (type === "tick") {
        const channels = event.data?.channels;
        if (channels && typeof channels === "object") {
          const record = channels as { ws?: number; poll?: number };
          setMeta({ channels: { ws: Number(record.ws ?? 0), poll: Number(record.poll ?? 0) } });
        }
        return;
      }

      if (event.device_uid) {
        void queryClient.invalidateQueries({ queryKey: ["device", event.device_uid] });
      }
      if (type === "device_updated" || type === "device_removed" || type === "uninstall_progress") {
        void queryClient.invalidateQueries({ queryKey: ["devices"] });
      }
      if (type === "frame_added" || type === "command_created" || type === "command_updated") {
        void queryClient.invalidateQueries({ queryKey: ["commands"] });
      }
      if (type === "notice" || type === "version") {
        void queryClient.invalidateQueries({ queryKey: ["updates"] });
        void queryClient.invalidateQueries({ queryKey: ["system"] });
      }
    };

    return openEventSocket(handle);
  }, [pushEvent, queryClient, setConnected, setMeta, syncTime]);

  return null;
}