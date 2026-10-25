import { Link } from "react-router-dom";
import { MonitorSmartphone } from "lucide-react";

import { GlowCard } from "@/components/aceternity/glow-card";
import { FrameView } from "@/components/frame-view";
import { Meter } from "@/components/meter";
import { StatusBadge } from "@/components/status-badge";
import { formatAge, formatBytes } from "@/lib/format";
import { isPollMode } from "@/lib/presence";
import type { Device } from "@/lib/types";
import { useViewport } from "@/hooks/use-viewport";
import { cn } from "@/lib/utils";

function numberFrom(source: Record<string, unknown>, key: string): number | null {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Chromium 81 dropped Tailwind's colour-mix based alpha gradients, so the
 * overlay is a literal rgba gradient (the documented decorative escape hatch).
 */
const THUMB_GRADIENT =
  "linear-gradient(to top, rgba(2, 6, 18, 0.62) 0%, rgba(2, 6, 18, 0.16) 46%, rgba(2, 6, 18, 0) 80%)";

interface Props {
  device: Device;
  /** `compact` is the watch card: fewer lines, larger touch target. */
  variant?: "full" | "compact";
}

/**
 * Fleet tile: a framed 16:10 thumbnail with a status pill, then name, meters and
 * one short meta line. The whole tile is the link, so the touch target is the
 * card rather than a small "open" affordance.
 *
 * Every number is the server's: `presence.age_ms` for age, `sysinfo.*` for the
 * meters, `latest_frame_id` for the thumbnail. Nothing is derived locally.
 */
export function DeviceCard({ device, variant = "full" }: Props) {
  const viewport = useViewport();
  const watch = viewport === "watch";
  const compact = variant === "compact";
  const cpu = numberFrom(device.sysinfo, "cpu_percent");
  const memory = numberFrom(device.sysinfo, "mem_percent");
  const memTotal = numberFrom(device.sysinfo, "mem_total");
  const offline = device.presence.state === "offline" || device.presence.state === "uninstalled";
  const pollMode = isPollMode(device.presence);

  const name = device.name || device.uid;
  const host = (device.hostname || "").trim();
  // A default-named device repeats its hostname twice; show the OS instead then.
  const hostIsRedundant = host === "" || host === name || host === device.uid;
  const secondary = hostIsRedundant
    ? device.os_name || ""
    : [host, device.os_name].filter(Boolean).join(" · ");
  const capacity = memTotal === null ? "" : formatBytes(memTotal);
  const metaLine = [capacity, device.arch].filter(Boolean).join(" · ");

  return (
    <Link
      to={`/device/${encodeURIComponent(device.uid)}`}
      aria-label={`打开设备 ${name}`}
      className={cn("block h-full min-w-0 rounded-panel no-underline", offline && "opacity-80")}
    >
      <GlowCard
        isStatic={watch}
        className="flex h-full min-w-0 flex-col"
        contentClassName="flex h-full flex-col"
        tint={offline ? "rgba(124, 136, 160, 0.14)" : "rgba(99, 146, 255, 0.20)"}
      >
        <div className="frame-stage ratio-16-10 relative w-full overflow-hidden rounded-xl bg-sunken">
          <div className="inset-full">
            {device.latest_frame_id ? (
              <FrameView
                uid={device.uid}
                frameId={device.latest_frame_id}
                variant="thumb"
                alt={`${name} 画面`}
              />
            ) : (
              <div className="flex h-full w-full flex-col items-center justify-center text-faint">
                <MonitorSmartphone className={watch ? "size-6" : "size-7"} aria-hidden />
                <span className={cn("mt-1", watch ? "text-[0.7rem]" : "text-[0.72rem]")}>还没有画面</span>
              </div>
            )}
          </div>

          <div className="pointer-events-none inset-full" style={{ backgroundImage: THUMB_GRADIENT }} />

          <div className="absolute left-2 top-2 flex min-w-0 items-center">
            <span
              className="inline-flex max-w-full rounded-full p-0.5"
              style={{ backgroundColor: "rgba(6, 10, 20, 0.48)" }}
            >
              <StatusBadge presence={device.presence} compact />
            </span>
          </div>
          <span
            className="absolute right-2 top-2 rounded-md border px-1.5 py-0.5 text-[0.7rem] font-semibold text-white"
            style={{ backgroundColor: "rgba(6, 10, 20, 0.62)", borderColor: "rgba(255, 255, 255, 0.12)" }}
          >
            v{device.agent_version || "?"}
          </span>

          {pollMode ? (
            <span
              className="absolute bottom-2 right-2 rounded-full px-2 py-0.5 text-[0.7rem] font-medium text-white"
              style={{ backgroundColor: "rgba(6, 10, 20, 0.66)" }}
            >
              仅轮询
            </span>
          ) : null}
        </div>

        <div className={cn("flex min-w-0 flex-1 flex-col", watch ? "mt-2.5" : "mt-3.5")}>
          <div className="flex min-w-0 items-start justify-between">
            <p
              className={cn(
                "min-w-0 truncate font-semibold tracking-tight text-ink",
                watch ? "text-[0.88rem]" : "text-[0.95rem]",
              )}
            >
              {name}
            </p>
            <span className={cn("tnum ml-2 shrink-0 text-faint", watch ? "text-[0.7rem]" : "text-[0.72rem]")}>
              {formatAge(device.presence.age_ms)}
            </span>
          </div>
          {secondary ? (
            <p className={cn("mt-1 min-w-0 truncate text-soft", watch ? "text-[0.72rem]" : "text-[0.8rem]")}>
              {secondary}
            </p>
          ) : null}

          <div className={cn("grid grid-cols-2 gap-x-3", watch ? "mt-2 gap-y-1" : "mt-3 gap-y-1.5")}>
            <Meter label="CPU" value={cpu} compact />
            <Meter label="内存" value={memory} compact />
          </div>

          {!compact && metaLine ? (
            <p className={cn("mt-2 min-w-0 truncate text-faint", watch ? "text-[0.69rem]" : "text-[0.7rem]")}>
              {metaLine}
            </p>
          ) : null}
        </div>
      </GlowCard>
    </Link>
  );
}
