import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { MonitorSmartphone, Plus, RefreshCw } from "lucide-react";

import { DeviceCard } from "@/components/device-card";
import { PageShell } from "@/components/page-shell";
import { Button, buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api, isApiError } from "@/lib/api";
import { isPollMode } from "@/lib/presence";
import { useViewport } from "@/hooks/use-viewport";
import { useLive } from "@/store/live";
import { cn } from "@/lib/utils";

/**
 * The wall（0.3.3 重写外壳）：统计改为胶囊芯片，卡片栅格三端分档。
 *
 * Every number and label here comes from the server's `presence` block: state
 * counts are `presence.state` tallies, "poll only" is `presence.transport`, and
 * the age on each card is `presence.age_ms`. The page never derives liveness and
 * never compares timestamps itself (spec C5, A12).
 */
export function WallPage() {
  const viewport = useViewport();
  const connected = useLive((state) => state.connected);

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["devices"],
    queryFn: api.listDevices,
    // The socket pushes changes; polling is the repair path. When it is down the
    // console polls faster so the wall still converges without the event stream.
    refetchInterval: connected ? 20_000 : 8_000,
  });

  const devices = data?.devices ?? [];
  const counts = {
    online: devices.filter((device) => device.presence.state === "online").length,
    degraded: devices.filter((device) => device.presence.state === "degraded").length,
    offline: devices.filter(
      (device) => device.presence.state === "offline" || device.presence.state === "uninstalled",
    ).length,
  };
  const pollOnly = devices.filter((device) => isPollMode(device.presence)).length;

  const columns =
    viewport === "watch"
      ? "grid-cols-1"
      : viewport === "mobile"
        ? "grid-cols-2"
        : devices.length >= 3
          ? "grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4"
          : "grid-cols-2 xl:grid-cols-3";

  const chips = [
    { tone: "bg-ok", label: `在线 ${counts.online}` },
    { tone: "bg-warn", label: `受限 ${counts.degraded}` },
    { tone: "bg-faint", label: `离线 ${counts.offline}` },
  ];

  return (
    <PageShell
      title="设备"
      subtitle={
        <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="font-mono text-[0.7rem] text-faint">v{data?.server_version ?? "—"}</span>
          {pollOnly > 0 ? (
            <span className="text-warn">{pollOnly} 台仅轮询：实时控制不可用，其余功能正常</span>
          ) : null}
          {!connected ? <span className="text-warn">实时连接断开，正在每 8 秒轮询一次</span> : null}
        </span>
      }
      actions={
        <Button variant="outline" size="sm" onClick={() => void refetch()} disabled={isFetching}>
          <RefreshCw className={cn("size-3.5", isFetching && "animate-spin")} />
          刷新
        </Button>
      }
    >
      <div className="flex flex-wrap gap-2">
        {chips.map((chip) => (
          <span
            key={chip.label}
            className="inline-flex items-center gap-1.5 rounded-full border border-line bg-panel px-3 py-1 text-xs font-medium text-soft shadow-card"
          >
            <span className={cn("inline-block size-1.5 rounded-full align-middle", chip.tone)} />
            {chip.label}
          </span>
        ))}
      </div>

      {isError ? (
        <div role="alert" className="rounded-panel border border-danger/30 bg-danger/10 p-3 text-xs text-danger">
          无法读取设备列表：{isApiError(error) ? error.message : "未知错误"}
          {isApiError(error) && error.retryable ? "（可点击刷新重试）" : null}
        </div>
      ) : null}

      {isLoading ? (
        <div className={cn("grid gap-3", columns)}>
          {[0, 1, 2].map((index) => (
            <Skeleton key={index} className="h-52 w-full rounded-panel" />
          ))}
        </div>
      ) : devices.length === 0 ? (
        <div className="flex flex-col items-center justify-center gap-3 rounded-panel border border-dashed border-line py-14 text-center">
          <MonitorSmartphone className="size-8 text-faint" />
          <div>
            <p className="text-sm font-medium text-ink">还没有受控端</p>
            <p className="mt-1 text-xs text-soft">在设置页获取安装程序，在目标机器上运行即可。</p>
          </div>
          <Link to="/settings" className={buttonVariants({ variant: "default", size: "sm" })}>
            <Plus className="size-3.5" />
            获取安装程序
          </Link>
        </div>
      ) : (
        <div className={cn("grid gap-3", columns)}>
          {devices.map((device) => (
            <DeviceCard key={device.uid} device={device} variant={viewport === "watch" ? "compact" : "full"} />
          ))}
        </div>
      )}
    </PageShell>
  );
}
