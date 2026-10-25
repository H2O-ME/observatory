import { Link } from "react-router-dom";
import { ArrowLeft, Camera, Cpu, MemoryStick, RadioTower, RefreshCw } from "lucide-react";

import { BackgroundBeams } from "@/components/aceternity/background-beams";
import { GridPattern } from "@/components/aceternity/grid-pattern";
import { GlowCard } from "@/components/aceternity/glow-card";
import { StatCard, StatGrid } from "@/components/stat-card";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { formatAge, formatBytes } from "@/lib/format";
import { transportOf } from "@/lib/presence";
import type { Device, TransportKind } from "@/lib/types";
import { useViewport } from "@/hooks/use-viewport";
import { cn } from "@/lib/utils";

function num(source: Record<string, unknown>, key: string): number | null {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

const TRANSPORT_LABEL: Record<TransportKind, string> = {
  ws: "实时 WS",
  poll: "HTTP 轮询",
  none: "无通道",
};

const CAPTURE_LABEL: Record<string, string> = {
  ok: "正常",
  starting: "启动中",
  no_session: "无会话",
  blocked: "受阻",
};

const STATE_LABEL: Record<string, string> = {
  online: "在线",
  degraded: "受限",
  offline: "离线",
  uninstalling: "卸载中",
  uninstalled: "已卸载",
};

function captureTone(state: string | null | undefined): "ok" | "warn" | "neutral" {
  if (!state) return "neutral";
  if (state === "ok") return "ok";
  if (state === "no_session" || state === "blocked") return "warn";
  return "neutral";
}

function loadTone(value: number | null): "brand" | "warn" | "danger" {
  if (value === null) return "brand";
  if (value >= 90) return "danger";
  if (value >= 75) return "warn";
  return "brand";
}

interface Props {
  device: Device;
  isFetching: boolean;
  onRefresh: () => void;
}

/**
 * Device identity strip.
 *
 * Desktop: a wide hero card plus four telemetry tiles.
 * Phone/watch: one compact row only — the tab content below carries the detail.
 * The old 2×2 telemetry block pushed the first actual content below the fold on
 * a 320px watch, which was the main "cramped" complaint.
 */
export function DeviceHero({ device, isFetching, onRefresh }: Props) {
  const viewport = useViewport();
  const watch = viewport === "watch";
  const desktop = viewport === "desktop";
  const transport = transportOf(device.presence);
  const capture = device.presence.capture;
  const cpu = num(device.sysinfo, "cpu_percent");
  const memory = num(device.sysinfo, "mem_percent");
  const memTotal = num(device.sysinfo, "mem_total");
  const reason = device.presence.state !== "online" ? device.presence.reason : "";

  if (!desktop) {
    return (
      <div className="surface flex min-w-0 items-center gap-2.5 p-2.5">
        <Link
          to="/wall"
          aria-label="返回设备墙"
          title="返回设备墙"
          className="inline-flex size-10 shrink-0 items-center justify-center rounded-xl border border-line bg-sunken text-soft no-underline transition-colors hover:text-ink"
        >
          <ArrowLeft className="size-4" aria-hidden />
        </Link>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <h1 className="min-w-0 truncate text-[0.98rem] font-semibold tracking-tight text-ink">
              {device.name || device.uid}
            </h1>
            <StatusBadge presence={device.presence} compact />
          </div>
          <p className={cn("mt-0.5 truncate text-soft", watch ? "text-[0.64rem]" : "text-[0.72rem]")}>
            <span className="font-mono">{device.hostname || device.uid}</span>
            <span className="mx-1 text-faint">·</span>v{device.agent_version || "?"}
            <span className="mx-1 text-faint">·</span>
            {formatAge(device.presence.age_ms)}
          </p>
          {reason ? (
            <p className={cn("mt-0.5 truncate text-warn", watch ? "text-[0.62rem]" : "text-[0.68rem]")} title={reason}>
              {reason}
            </p>
          ) : null}
        </div>
        <Button
          variant="outline"
          size="icon-lg"
          className="shrink-0"
          onClick={onRefresh}
          aria-label="刷新设备信息"
          title="刷新设备信息"
        >
          <RefreshCw className={cn("size-4", isFetching && "animate-spin")} aria-hidden />
        </Button>
      </div>
    );
  }

  return (
    <div className="min-w-0 space-y-3">
      <GlowCard className="min-w-0" contentClassName="p-5" tint="rgba(99, 146, 255, 0.18)">
        <GridPattern fade="radial" className="opacity-50" stroke="var(--c-grid-line)" strokeDasharray="4 6" />
        <BackgroundBeams count={8} className="opacity-35" />
        <div className="relative z-10 flex min-w-0 items-start">
          <Link
            to="/wall"
            aria-label="返回设备墙"
            title="返回设备墙"
            className="mr-3 inline-flex size-11 shrink-0 items-center justify-center rounded-xl border border-line bg-sunken text-soft no-underline transition-colors hover:text-ink"
          >
            <ArrowLeft className="size-[1.1rem]" aria-hidden />
          </Link>
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 flex-wrap items-center">
              <h1 className="mr-3 min-w-0 break-words text-[1.7rem] font-semibold leading-tight tracking-tight text-ink">
                {device.name || device.uid}
              </h1>
              <StatusBadge presence={device.presence} large />
            </div>
            <p className="mt-1.5 min-w-0 break-words text-[0.84rem] leading-relaxed text-soft">
              <span className="font-mono">{device.hostname || device.uid}</span>
              <span className="mx-1.5 text-faint">·</span>v{device.agent_version || "?"}
              <span className="mx-1.5 text-faint">·</span>
              {formatAge(device.presence.age_ms)}
              {device.protocol ? (
                <>
                  <span className="mx-1.5 text-faint">·</span>协议 {device.protocol}
                </>
              ) : null}
            </p>
            {reason ? (
              <p className="mt-1 truncate text-[0.74rem] text-warn" title={reason}>
                {reason}
              </p>
            ) : null}
          </div>
          <Button
            variant="outline"
            size="icon-sm"
            className="ml-3 shrink-0"
            onClick={onRefresh}
            aria-label="刷新设备信息"
            title="刷新设备信息"
          >
            <RefreshCw className={cn("size-4", isFetching && "animate-spin")} aria-hidden />
          </Button>
        </div>
      </GlowCard>

      <StatGrid columns={4}>
        <StatCard
          label="通道"
          value={TRANSPORT_LABEL[transport]}
          hint={STATE_LABEL[device.presence.state] ?? device.presence.state}
          tone={transport === "ws" ? "ok" : transport === "poll" ? "warn" : "neutral"}
          icon={RadioTower}
        />
        <StatCard
          label="截屏"
          value={capture ? CAPTURE_LABEL[capture.state] ?? (capture.state || "—") : "—"}
          hint={capture?.reason || "采集健康"}
          tone={captureTone(capture?.state)}
          icon={Camera}
        />
        <StatCard
          label="CPU"
          value={cpu === null ? "—" : `${cpu.toFixed(0)}%`}
          hint="处理器占用"
          tone={loadTone(cpu)}
          icon={Cpu}
        />
        <StatCard
          label="内存"
          value={memory === null ? "—" : `${memory.toFixed(0)}%`}
          hint={memTotal === null ? "未上报总量" : `共 ${formatBytes(memTotal)}`}
          tone={loadTone(memory)}
          icon={MemoryStick}
        />
      </StatGrid>
    </div>
  );
}
