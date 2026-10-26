import { Link } from "react-router-dom";
import { MonitorSmartphone } from "lucide-react";

import { FrameView } from "@/components/frame-view";
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

/** Literal rgba scrim: Chromium 81 cannot parse Tailwind's color-mix overlay. */
const THUMB_GRADIENT =
  "linear-gradient(to top, rgba(2, 6, 18, 0.58) 0%, rgba(2, 6, 18, 0.12) 42%, rgba(2, 6, 18, 0) 74%)";

const CHIP_BG = { backgroundColor: "rgba(6, 10, 20, 0.62)" };

interface Props {
  device: Device;
  /** `compact` is the watch card: fewer lines, larger touch target. */
  variant?: "full" | "compact";
}

function metricTone(value: number | null): string {
  if (value === null) return "text-faint";
  if (value >= 90) return "text-destructive";
  if (value >= 75) return "text-warning";
  return "text-foreground";
}

/**
 * One device in the wall.
 *
 * The thumbnail is the only framed element; identity and telemetry sit on the
 * canvas underneath. That keeps six devices from reading as six identical cards
 * and leaves the screen itself as the visual anchor.
 *
 * Every number is the server's: `presence.age_ms` for age, `sysinfo.*` for the
 * metrics, `latest_frame_id` for the thumbnail. Nothing is derived locally.
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

  return (
    <Link
      to={`/device/${encodeURIComponent(device.uid)}`}
      aria-label={`打开设备 ${name}`}
      className="device-tile group block min-w-0 no-underline"
    >
      <div
        className={cn(
          "frame-stage ratio-16-10 relative w-full overflow-hidden rounded-md border border-border transition-colors group-hover:border-border-strong",
          offline && "opacity-75",
        )}
      >
        <div className="thumb-zoom absolute-fill">
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
              <span className={cn("mt-1", watch ? "text-xs" : "text-xs")}>
                还没有画面
              </span>
            </div>
          )}
        </div>

        <div
          className="pointer-events-none absolute-fill"
          style={{ backgroundImage: THUMB_GRADIENT }}
          aria-hidden
        />

        <div className="absolute top-2 left-2 flex min-w-0 items-center">
          <span className="inline-flex max-w-full rounded-sm p-0.5" style={CHIP_BG}>
            <StatusBadge presence={device.presence} compact />
          </span>
        </div>

        <span
          className="tnum absolute top-2 right-2 rounded-sm px-1.5 py-0.5 font-mono text-xs font-medium text-white"
          style={CHIP_BG}
        >
          v{device.agent_version || "?"}
        </span>

        {pollMode ? (
          <span
            className="absolute bottom-2 left-2 rounded-sm px-1.5 py-0.5 text-xs font-medium text-white"
            style={CHIP_BG}
          >
            仅轮询
          </span>
        ) : null}
      </div>

      <div className={cn("min-w-0", watch ? "mt-2" : "mt-2.5")}>
        <div className="flex min-w-0 items-baseline justify-between gap-2">
          <p
            className={cn(
              "min-w-0 truncate font-medium tracking-tight text-foreground",
              watch ? "text-sm" : "text-sm",
            )}
          >
            {name}
          </p>
          <span className="tnum shrink-0 text-xs text-faint">
            {formatAge(device.presence.age_ms)}
          </span>
        </div>

        {secondary ? (
          <p className="mt-0.5 min-w-0 truncate text-xs text-muted-foreground">{secondary}</p>
        ) : null}

        {compact ? null : (
          <div className="mt-2 flex min-w-0 items-center gap-3 text-xs text-muted-foreground">
            <span className="shrink-0">
              CPU <span className={cn("tnum font-medium", metricTone(cpu))}>{cpu === null ? "—" : `${cpu.toFixed(0)}%`}</span>
            </span>
            <span className="shrink-0">
              内存 <span className={cn("tnum font-medium", metricTone(memory))}>{memory === null ? "—" : `${memory.toFixed(0)}%`}</span>
            </span>
            {capacity ? (
              <span className="ml-auto min-w-0 truncate text-xs text-faint">{capacity}</span>
            ) : null}
          </div>
        )}
      </div>
    </Link>
  );
}
