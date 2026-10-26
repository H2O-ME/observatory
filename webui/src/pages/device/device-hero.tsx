import { Link } from "react-router-dom";
import { ArrowLeft, RefreshCw } from "lucide-react";

import { StatCard } from "@/components/stat-card";
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

function captureTone(state: string | null | undefined): "warn" | "neutral" {
  if (state === "no_session" || state === "blocked") return "warn";
  return "neutral";
}

function loadTone(value: number | null): "warn" | "danger" | "neutral" {
  if (value === null) return "neutral";
  if (value >= 90) return "danger";
  if (value >= 75) return "warn";
  return "neutral";
}

interface Props {
  device: Device;
  isFetching: boolean;
  onRefresh: () => void;
}

/**
 * Device identity + telemetry strip.
 *
 * The identity block is typography, not a card. Desktop adds one metric strip;
 * phone and watch show the identity row only, because the tab content below
 * already carries the detail.
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
      <div className="flex min-w-0 items-start gap-2.5">
        <Link
          to="/wall"
          aria-label="返回设备墙"
          title="返回设备墙"
          className="inline-flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-card text-muted-foreground no-underline transition-colors hover:text-foreground"
        >
          <ArrowLeft className="size-4 stroke-[1.5]" aria-hidden />
        </Link>
        <div className="min-w-0 flex-1">
          <div className="flex min-h-8 min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <h1
              className={cn(
                "min-w-0 truncate leading-8 font-semibold tracking-[-0.02em]",
                watch ? "text-base" : "text-lg",
              )}
            >
              {device.name || device.uid}
            </h1>
            <StatusBadge presence={device.presence} compact />
          </div>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">
            <span className="font-mono">{device.hostname || device.uid}</span>
            <span className="mx-1.5 text-faint">·</span>v{device.agent_version || "?"}
            <span className="mx-1.5 text-faint">·</span>
            {formatAge(device.presence.age_ms)}
          </p>
          {reason ? (
            <p className="mt-0.5 truncate text-xs text-warning" title={reason}>
              {reason}
            </p>
          ) : null}
        </div>
        <Button
          variant="outline"
          size="icon-sm"
          className="shrink-0"
          onClick={onRefresh}
          aria-label="刷新设备信息"
          title="刷新设备信息"
        >
          <RefreshCw className={cn("size-4 stroke-[1.5]", isFetching && "animate-spin")} aria-hidden />
        </Button>
      </div>
    );
  }

  return (
    <div className="min-w-0 space-y-5">
      <div className="flex min-w-0 items-start justify-between gap-4">
        <div className="flex min-w-0 items-start gap-3">
          <Link
            to="/wall"
            aria-label="返回设备墙"
            title="返回设备墙"
            className="inline-flex size-9 shrink-0 items-center justify-center rounded-md border border-border bg-card text-muted-foreground no-underline transition-colors hover:text-foreground"
          >
            <ArrowLeft className="size-4 stroke-[1.5]" aria-hidden />
          </Link>
          <div className="min-w-0">
            <div className="flex min-h-9 min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
              <h1 className="min-w-0 break-words text-2xl leading-9 font-semibold tracking-[-0.025em] text-foreground">
                {device.name || device.uid}
              </h1>
              <StatusBadge presence={device.presence} />
            </div>
            <p className="mt-0.5 min-w-0 break-words text-xs leading-relaxed text-muted-foreground">
              <span className="font-mono">{device.hostname || device.uid}</span>
              <span className="mx-2 text-faint">·</span>v{device.agent_version || "?"}
              <span className="mx-2 text-faint">·</span>
              {formatAge(device.presence.age_ms)}
              {device.protocol ? (
                <>
                  <span className="mx-2 text-faint">·</span>协议 {device.protocol}
                </>
              ) : null}
            </p>
            {reason ? (
              <p className="mt-1 truncate text-xs text-warning" title={reason}>
                {reason}
              </p>
            ) : null}
          </div>
        </div>
        <Button
          variant="ghost"
          className="shrink-0"
          onClick={onRefresh}
          aria-label="刷新设备信息"
          title="刷新设备信息"
        >
          <RefreshCw className={cn("size-4 stroke-[1.5]", isFetching && "animate-spin")} aria-hidden />
          刷新
        </Button>
      </div>

      <div className="grid min-w-0 grid-cols-12 gap-6">
        <StatCard
          className="col-span-3"
          label="通道"
          value={TRANSPORT_LABEL[transport]}
          hint={STATE_LABEL[device.presence.state] ?? device.presence.state}
          tone={transport === "poll" ? "warn" : "neutral"}
        />
        <StatCard
          className="col-span-3"
          label="截屏"
          value={capture ? CAPTURE_LABEL[capture.state] ?? (capture.state || "—") : "—"}
          hint={capture?.reason || "采集健康"}
          tone={captureTone(capture?.state)}
        />
        <StatCard
          className="col-span-3"
          label="CPU"
          value={cpu === null ? "—" : `${cpu.toFixed(0)}%`}
          hint="处理器占用"
          tone={loadTone(cpu)}
        />
        <StatCard
          className="col-span-3"
          label="内存"
          value={memory === null ? "—" : `${memory.toFixed(0)}%`}
          hint={memTotal === null ? "未上报总量" : `共 ${formatBytes(memTotal)}`}
          tone={loadTone(memory)}
        />
      </div>
    </div>
  );
}
