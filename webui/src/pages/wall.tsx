import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Activity,
  MonitorSmartphone,
  Plus,
  RadioTower,
  RefreshCw,
  ShieldAlert,
  WifiOff,
} from "lucide-react";

import { DeviceCard } from "@/components/device-card";
import { PageHeader } from "@/components/page-header";
import { StatCard, StatGrid } from "@/components/stat-card";
import { ErrorBlock } from "@/components/async-state";
import { Button, buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api, isApiError } from "@/lib/api";
import { isPollMode } from "@/lib/presence";
import { useViewport } from "@/hooks/use-viewport";
import { useLive } from "@/store/live";
import { cn } from "@/lib/utils";

/**
 * The wall（0.4 重写）：任务控制台式的设备墙。
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

  // Desktop reads as a 3-up instrument shelf at the acceptance width; phones and
  // the watch are strictly single column so nothing horizontally overflows.
  const columns = desktop ? "grid-cols-2 xl:grid-cols-3" : "grid-cols-1";
  const retryable = isApiError(error) && Boolean(error.retryable);

  const description = (
    <span className="flex min-w-0 flex-wrap items-baseline">
      <span className="tnum mr-2">v{data?.server_version ?? "—"}</span>
      <span className="mr-2 text-faint" aria-hidden>
        ·
      </span>
      <span className="tnum">{isLoading ? "…" : `${devices.length} 台受控端`}</span>
      {pollOnly > 0 ? (
        <>
          <span className="mx-2 text-faint" aria-hidden>
            ·
          </span>
          <span className="tnum text-warn">{pollOnly} 台仅轮询</span>
        </>
      ) : null}
    </span>
  );

  return (
    <div className="min-w-0 space-y-3 sm:space-y-4">
      <PageHeader
        title="设备墙"
        description={description}
        actions={
          <Button
            variant="outline"
            size="sm"
            className={cn(!desktop && "tap")}
            onClick={() => void refetch()}
            disabled={isFetching}
            aria-label="刷新设备列表"
            title="刷新设备列表"
          >
            <RefreshCw className={cn("size-3.5", isFetching && "animate-spin")} aria-hidden />
            刷新
          </Button>
        }
      />

      <StatGrid columns={4} className="stagger">
        <StatCard label="在线" value={isLoading ? "—" : counts.online} tone="ok" icon={Activity} />
        <StatCard label="受限" value={isLoading ? "—" : counts.degraded} tone="warn" icon={ShieldAlert} />
        <StatCard label="离线" value={isLoading ? "—" : counts.offline} tone="neutral" icon={WifiOff} />
        <StatCard label="仅轮询" value={isLoading ? "—" : pollOnly} tone="brand" icon={RadioTower} />
      </StatGrid>

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
        <div className={cn("grid min-w-0 gap-4 sm:gap-5", columns)}>
          {[0, 1, 2].map((index) => (
            <div key={index} className="surface min-w-0 overflow-hidden">
              <div className="ratio-16-10 w-full animate-pulse bg-sunken" />
              <div className="space-y-2 p-3.5">
                <Skeleton className="h-3.5 w-2/3" />
                <Skeleton className="h-3 w-1/2" />
                <div className="grid grid-cols-2 gap-3 pt-1">
                  <Skeleton className="h-2 w-full" />
                  <Skeleton className="h-2 w-full" />
                </div>
              </div>
            </div>
          ))}
        </div>
      ) : devices.length === 0 ? (
        <div className="surface relative flex min-w-0 flex-col items-center justify-center overflow-hidden px-4 py-10 text-center sm:py-14">
          <div className="relative z-10 flex min-w-0 flex-col items-center">
            <span
              className={cn(
                "flex items-center justify-center rounded-md border border-line bg-sunken text-soft",
                watch ? "size-10" : "size-11",
              )}
            >
              <MonitorSmartphone className="size-5" aria-hidden />
            </span>
            <p className={cn("mt-3 font-semibold text-ink", watch ? "text-[0.9rem]" : "text-[0.95rem]")}>
              还没有受控端
            </p>
            <p className={cn("mt-1 max-w-sm leading-relaxed text-soft", watch ? "text-[0.72rem]" : "text-[0.8rem]")}>
              在设置页获取安装程序，在目标机器上运行即可接入控制台。
            </p>
            <Link
              to="/settings"
              className={cn(buttonVariants({ size: "sm" }), "mt-4 no-underline", !desktop && "tap")}
            >
              <Plus className="size-4" aria-hidden />
              获取安装程序
            </Link>
          </div>
        </div>
      ) : (
        <div className={cn("stagger grid min-w-0 gap-4 sm:gap-5", columns)}>
          {devices.map((device) => (
            <DeviceCard
              key={device.uid}
              device={device}
              variant={watch ? "compact" : "full"}
            />
          ))}
        </div>
      )}
    </div>
  );
}
