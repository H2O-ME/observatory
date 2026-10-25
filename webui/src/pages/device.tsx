import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, RefreshCw, WifiOff } from "lucide-react";

import { StatusBadge } from "@/components/status-badge";
import { CommandsView } from "@/pages/device/commands-view";
import { FilesView } from "@/pages/device/files-view";
import { LiveView } from "@/pages/device/live-view";
import { OverviewView } from "@/pages/device/overview-view";
import { DeviceSettingsView } from "@/pages/device/settings-view";
import { TimelineView } from "@/pages/device/timeline-view";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, isApiError } from "@/lib/api";
import { formatAge } from "@/lib/format";
import { isPollMode } from "@/lib/presence";
import { useViewport } from "@/hooks/use-viewport";
import { useLive } from "@/store/live";
import { cn } from "@/lib/utils";

const TABS = [
  { value: "overview", label: "概览" },
  { value: "live", label: "实时" },
  { value: "timeline", label: "时间线" },
  { value: "files", label: "文件" },
  { value: "commands", label: "命令" },
  { value: "settings", label: "设置" },
] as const;

type TabValue = (typeof TABS)[number]["value"];

function isTabValue(value: string): value is TabValue {
  return TABS.some((tab) => tab.value === value);
}

/**
 * Device shell（0.3.3 重写外壳）。
 *
 * The active tab lives in the URL (`/device/:uid/<tab>`), so a link to the live
 * view stays a link to the live view after a reload. The 0.2.x URLs (`/` and
 * `/devices/:uid`) are served by the same component. Tab 触发区按三端放大：
 * 手表 44px、手机 40px，保证圆屏与指触可用。
 */
export function DevicePage() {
  const params = useParams();
  const uid = params.uid ?? "";
  const splat = params["*"] ?? "";
  const requestedTab = splat.split("/")[0] ?? "";
  const tab: TabValue = isTabValue(requestedTab) ? requestedTab : "overview";
  const navigate = useNavigate();
  const viewport = useViewport();
  const connected = useLive((state) => state.connected);
  const watch = viewport === "watch";

  const query = useQuery({
    queryKey: ["device", uid],
    queryFn: () => api.getDevice(uid),
    enabled: Boolean(uid),
    refetchInterval: connected ? 10_000 : 4_000,
  });

  const device = query.data?.device;
  const spacing = watch ? "space-y-2" : viewport === "mobile" ? "space-y-3" : "space-y-4";

  const selectTab = (value: string) => {
    const next: TabValue = isTabValue(value) ? value : "overview";
    navigate(`/device/${encodeURIComponent(uid)}/${next}`);
  };

  if (query.isError) {
    return (
      <div className="space-y-3">
        <Link to="/wall" className="inline-flex items-center gap-1 text-xs text-soft no-underline">
          <ChevronLeft className="size-3.5" /> 返回设备列表
        </Link>
        <div role="alert" className="rounded-panel border border-danger/30 bg-danger/10 p-3 text-xs text-danger">
          无法读取设备：{isApiError(query.error) ? query.error.message : "未知错误"}
        </div>
      </div>
    );
  }

  if (!device) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-20 w-full rounded-panel" />
        <Skeleton className="h-64 w-full rounded-panel" />
      </div>
    );
  }

  return (
    <div className={spacing}>
      <div className="flex items-start gap-2.5">
        <Link
          to="/wall"
          className={cn(
            "mt-0.5 inline-flex size-9 shrink-0 items-center justify-center rounded-lg border border-line bg-panel text-soft no-underline shadow-card",
            watch && "size-10",
          )}
          aria-label="返回设备列表"
        >
          <ChevronLeft className="size-[1.1rem]" />
        </Link>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <h1
              className={cn(
                "min-w-0 truncate font-semibold tracking-tight text-ink",
                watch ? "text-[0.95rem]" : viewport === "mobile" ? "text-[1.05rem]" : "text-lg",
              )}
            >
              {device.name || device.uid}
            </h1>
            <StatusBadge presence={device.presence} />
          </div>
          <p className={cn("mt-0.5 truncate text-soft", watch ? "text-[0.68rem]" : "text-xs")}>
            {device.hostname} · v{device.agent_version || "?"}
            {device.protocol ? ` · 协议 ${device.protocol}` : ""} · {formatAge(device.presence.age_ms)}
          </p>
        </div>
        <Button
          variant="outline"
          size="icon-sm"
          className="shrink-0"
          onClick={() => void query.refetch()}
          aria-label="刷新"
        >
          <RefreshCw className={cn("size-3.5", query.isFetching && "animate-spin")} />
        </Button>
      </div>

      {isPollMode(device.presence) ? (
        <div className="rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-xs leading-relaxed text-warn">
          <p className="flex items-center gap-1.5 font-medium">
            <WifiOff className="size-3.5 shrink-0" aria-hidden />
            当前为轮询模式，实时控制不可用
          </p>
          <p className="mt-0.5 font-normal">
            {device.presence.reason ? `${device.presence.reason}。` : ""}
            设备通过 HTTP 轮询上报，截图、文件管理、命令下发和历史画面仍然可用，只是响应更慢；受控端恢复实时通道后会自动切回。
          </p>
        </div>
      ) : device.presence.state !== "online" ? (
        <div className="rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-xs leading-relaxed text-warn">
          {device.presence.reason}
        </div>
      ) : null}

      <Tabs value={tab} onValueChange={selectTab}>
        <div className="-mx-1 overflow-x-auto px-1 scrollbar-none">
          <TabsList className="w-max">
            {TABS.map((item) => (
              <TabsTrigger
                key={item.value}
                value={item.value}
                className={cn("px-3.5", watch ? "min-h-11 text-[0.8rem]" : "min-h-10 text-[0.85rem]")}
              >
                {item.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>

        <TabsContent value="overview" className="mt-3">
          <OverviewView device={device} onRefresh={() => void query.refetch()} />
        </TabsContent>
        <TabsContent value="live" className="mt-3">
          <LiveView device={device} />
        </TabsContent>
        <TabsContent value="timeline" className="mt-3">
          <TimelineView device={device} />
        </TabsContent>
        <TabsContent value="files" className="mt-3">
          <FilesView device={device} />
        </TabsContent>
        <TabsContent value="commands" className="mt-3">
          <CommandsView device={device} />
        </TabsContent>
        <TabsContent value="settings" className="mt-3">
          <DeviceSettingsView device={device} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
