import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { MonitorSmartphone, Plus, RefreshCw } from "lucide-react";

import { DeviceCard } from "@/components/device-card";
import { PageHeader } from "@/components/page-header";
import { StatCell, StatStrip } from "@/components/stat-card";
import { EmptyBlock, ErrorBlock } from "@/components/async-state";
import { Button, buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api, isApiError } from "@/lib/api";
import { isPollMode } from "@/lib/presence";
import { useViewport } from "@/hooks/use-viewport";
import { useLive } from "@/store/live";
import { cn } from "@/lib/utils";

/**
 * The wall: fleet overview.
 *
 * Every number and label here comes from the server's `presence` block: state
 * counts are `presence.state` tallies, "poll only" is `presence.transport`, and
 * the age on each card is `presence.age_ms`. The page never derives liveness and
 * never compares timestamps itself (spec C5, A12).
 */
export function WallPage() {
  const viewport = useViewport();
  const connected = useLive((state) => state.connected);
  const watch = viewport === "watch";
  const desktop = viewport === "desktop";

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

  // Desktop reads as a 3-up shelf at the acceptance width; phones and the watch
  // are strictly single column so nothing horizontally overflows.
  const columns = desktop
    ? "grid-cols-2 gap-x-5 gap-y-6 xl:grid-cols-3"
    : "grid-cols-1 gap-y-5";
  const retryable = isApiError(error) && Boolean(error.retryable);

  const description = (
    <span className="flex min-w-0 flex-wrap items-baseline gap-x-1.5">
      <span className="tnum">v{data?.server_version ?? "—"}</span>
      <span className="text-faint" aria-hidden>
        ·
      </span>
      <span className="tnum">{isLoading ? "…" : `${devices.length} 台受控端`}</span>
      {pollOnly > 0 ? (
        <>
          <span className="text-faint" aria-hidden>
            ·
          </span>
          <span className="tnum text-warning">{pollOnly} 台仅轮询</span>
        </>
      ) : null}
    </span>
  );

  return (
    <div className="min-w-0 space-y-5">
      <PageHeader
        title="设备墙"
        description={description}
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={() => void refetch()}
            disabled={isFetching}
            aria-label="刷新设备列表"
          >
            <RefreshCw className={cn("size-3.5", isFetching && "animate-spin")} aria-hidden />
            刷新
          </Button>
        }
      />

      <StatStrip>
        <StatCell label="在线" value={isLoading ? "—" : counts.online} hint="有活动实时通道" />
        <StatCell label="受限" value={isLoading ? "—" : counts.degraded} tone="warn" hint="通道降级或受限" />
        <StatCell label="离线" value={isLoading ? "—" : counts.offline} hint="含已卸载" />
        <StatCell label="仅轮询" value={isLoading ? "—" : pollOnly} tone={pollOnly > 0 ? "warn" : "neutral"} hint="无实时控制" />
      </StatStrip>

      {isError ? (
        <ErrorBlock
          message={`无法读取设备列表：${isApiError(error) ? error.message : "未知错误"}${
            retryable ? "（可点击重试）" : ""
          }`}
          retryable={retryable}
          onRetry={() => void refetch()}
        />
      ) : null}

      {isLoading ? (
        <div className={cn("grid min-w-0", columns)}>
          {[0, 1, 2].map((index) => (
            <div key={index} className="min-w-0">
              <Skeleton className="ratio-16-10 w-full rounded-md" />
              <div className="mt-2.5 space-y-1.5">
                <Skeleton className="h-3.5 w-2/3" />
                <Skeleton className="h-3 w-1/2" />
                <Skeleton className="h-2.5 w-1/3" />
              </div>
            </div>
          ))}
        </div>
      ) : devices.length === 0 ? (
        <EmptyBlock
          icon={MonitorSmartphone}
          title="还没有受控端"
          description="在设置页获取安装程序，在目标机器上运行即可接入控制台。"
          action={
            <Link
              to="/settings"
              className={cn(buttonVariants({ size: "sm" }), "no-underline")}
            >
              <Plus className="size-4" aria-hidden />
              获取安装程序
            </Link>
          }
          className={cn("border-solid", watch && "p-5")}
        />
      ) : (
        <div className="min-w-0">
          <div className="mb-3 flex items-baseline justify-between gap-3">
            <h2 className="text-xs font-medium tracking-[0.08em] text-muted-foreground uppercase">
              受控端
            </h2>
            <span className="tnum text-xs text-faint">
              {devices.length} 台 · {connected ? "实时更新" : "轮询更新"}
            </span>
          </div>
          <div className={cn("grid min-w-0", columns)}>
            {devices.map((device) => (
              <DeviceCard key={device.uid} device={device} variant={watch ? "compact" : "full"} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
