import { Link } from "react-router-dom";
import { Cpu, HardDrive, MemoryStick, MonitorSmartphone, WifiOff } from "lucide-react";

import { FrameStage, FrameView } from "@/components/frame-view";
import { Meter } from "@/components/meter";
import { StatusBadge } from "@/components/status-badge";
import { formatAge, formatBytes } from "@/lib/format";
import { isPollMode } from "@/lib/presence";
import type { Device } from "@/lib/types";
import { useAge } from "@/hooks/use-server-now";
import { cn } from "@/lib/utils";

function numberFrom(source: Record<string, unknown>, key: string): number | null {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function textFrom(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === "string" ? value : "";
}

interface Props {
  device: Device;
  stale: boolean;
  /** `compact` is the phone/watch card: fewer lines, larger touch target. */
  variant?: "full" | "compact";
}

export function DeviceCard({ device, stale, variant = "full" }: Props) {
  const cpu = numberFrom(device.sysinfo, "cpu_percent");
  const memory = numberFrom(device.sysinfo, "mem_percent");
  const memTotal = numberFrom(device.sysinfo, "mem_total");
  const age = useAge(device.last_seen_ms);
  const offline = device.presence.state === "offline" || device.presence.state === "uninstalled";
  // Reachable but poll-only: the card must say why live control is missing,
  // otherwise "受限" looks like an arbitrary label. Kept separate from the
  // capture-blocked banner on the detail page — different fault, different fix.
  const pollMode = isPollMode(device.presence);

  return (
    <Link
      to={`/devices/${encodeURIComponent(device.uid)}`}
      className={cn(
        "group flex min-w-0 flex-col gap-2 rounded-panel border border-line bg-panel p-3 no-underline shadow-card transition-colors active:bg-sunken",
        "hover:border-line-strong",
        offline && "opacity-80",
      )}
    >
      <FrameStage className="aspect-[16/10] w-full">
        {device.latest_frame_id ? (
          <FrameView uid={device.uid} frameId={device.latest_frame_id} variant="thumb" alt={`${device.name} 画面`} />
        ) : (
          <div className="flex h-full w-full flex-col items-center justify-center gap-1 text-faint">
            <MonitorSmartphone className="size-6" />
            <span className="text-[0.7rem]">还没有画面</span>
          </div>
        )}
        <div className="pointer-events-none absolute inset-x-0 bottom-0 flex items-end justify-between gap-2 bg-gradient-to-t from-black/55 to-transparent p-2">
          <StatusBadge presence={device.presence} stale={stale} compact />
          <span className="shrink-0 rounded bg-black/45 px-1.5 py-0.5 text-[0.65rem] font-medium text-white/90">
            v{device.agent_version || "?"}
          </span>
        </div>
      </FrameStage>

      <div className="flex min-w-0 items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-ink">{device.name || device.uid}</p>
          <p className="truncate text-[0.7rem] text-soft">
            {device.hostname || device.uid}
            {device.os_name ? ` · ${device.os_name}` : ""}
          </p>
        </div>
        <span className="shrink-0 text-[0.68rem] text-faint">{formatAge(age)}</span>
      </div>

      {pollMode ? (
        <p className="flex items-center gap-1 rounded-md border border-warn/25 bg-warn/10 px-1.5 py-1 text-[0.66rem] leading-tight text-warn">
          <WifiOff className="size-3 shrink-0" aria-hidden />
          <span className="truncate">
            {variant === "compact" ? "仅轮询 · 无实时控制" : "轮询模式 · 实时控制不可用"}
          </span>
        </p>
      ) : null}

      {variant === "full" ? (
        <div className="grid grid-cols-2 gap-x-3 gap-y-1.5">
          <Meter label="CPU" value={cpu} compact />
          <Meter label="内存" value={memory} compact />
          <div className="col-span-2 flex items-center gap-1 text-[0.68rem] text-faint">
            <MemoryStick className="size-3" />
            <span className="truncate">{memTotal ? formatBytes(memTotal) : "—"}</span>
            <Cpu className="ml-1 size-3" />
            <span className="truncate">{device.arch || "—"}</span>
            <HardDrive className="ml-1 size-3" />
            <span className="truncate">{textFrom(device.sysinfo, "username") || "—"}</span>
          </div>
        </div>
      ) : null}
    </Link>
  );
}