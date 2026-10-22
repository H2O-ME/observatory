import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft, RefreshCw, WifiOff } from "lucide-react";

import { StatusBadge } from "@/components/status-badge";
import { CommandsView } from "@/pages/device/commands-view";
import { FilesView } from "@/pages/device/files-view";
import { LiveView } from "@/pages/device/live-view";
import { OverviewView } from "@/pages/device/overview-view";
import { TimelineView } from "@/pages/device/timeline-view";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { formatAge } from "@/lib/format";
import { isPollMode } from "@/lib/presence";
import { useAge } from "@/hooks/use-server-now";
import { useViewport } from "@/hooks/use-viewport";
import { useLive } from "@/store/live";
import { cn } from "@/lib/utils";

const TABS = [
  { value: "overview", label: "概览" },
  { value: "live", label: "实时" },
  { value: "timeline", label: "时间线" },
  { value: "files", label: "文件" },
  { value: "commands", label: "命令" },
];

export function DevicePage() {
  const { uid = "" } = useParams();
  const viewport = useViewport();
  const connected = useLive((state) => state.connected);
  const [tab, setTab] = useState("overview");

  const query = useQuery({
    queryKey: ["device", uid],
    queryFn: () => api.device(uid),
    enabled: Boolean(uid),
    refetchInterval: connected ? 10_000 : 4_000,
  });

  const device = query.data?.device;
  const age = useAge(device?.last_seen_ms);
  const shellPadding = useMemo(
    () => (viewport === "watch" ? "space-y-2" : viewport === "mobile" ? "space-y-3" : "space-y-4"),
    [viewport],
  );

  if (query.isError) {
    return (
      <div className="space-y-3">
        <Link to="/" className="inline-flex items-center gap-1 text-xs text-soft no-underline">
          <ChevronLeft className="size-3.5" /> 返回设备列表
        </Link>
        <div className="rounded-panel border border-danger/30 bg-danger/10 p-3 text-xs text-danger">
          无法读取设备：{query.error instanceof Error ? query.error.message : "未知错误"}
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
    <div className={shellPadding}>
      <div className="flex items-start gap-2">
        <Link
          to="/"
          className="mt-0.5 inline-flex size-9 shrink-0 items-center justify-center rounded-lg border border-line text-soft no-underline"
          aria-label="返回"
        >
          <ChevronLeft className="size-4" />
        </Link>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <h1 className="truncate text-base font-semibold text-ink">{device.name || device.uid}</h1>
            <StatusBadge presence={device.presence} stale={!connected} />
          </div>
          <p className="mt-0.5 truncate text-[0.7rem] text-soft">
            {device.hostname} · v{device.agent_version || "?"} · {formatAge(age)}
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

      {/*
        A poll-only device is reachable and can run every command, but live
        control needs the WebSocket. Saying only "受限" made operators think the
        console was broken. This banner names the mode and what still works;
        the capture-blocked banner on the overview tab stays separate because it
        is a different fault (screen, not transport).
      */}
      {isPollMode(device.presence) ? (
        <div className="rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-[0.72rem] leading-relaxed text-warn">
          <p className="flex items-center gap-1.5 font-medium">
            <WifiOff className="size-3.5 shrink-0" aria-hidden />
            当前为轮询模式，实时控制不可用
          </p>
          <p className="mt-0.5 font-normal">
            {device.presence.reason ? `${device.presence.reason}。` : ""}
            设备通过 HTTPS 轮询上报，截图、文件管理、命令下发和历史画面仍然可用，只是响应更慢；受控端恢复 WebSocket 通道后会自动切回实时控制。
          </p>
        </div>
      ) : device.presence.state !== "online" ? (
        <div className="rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-[0.72rem] leading-relaxed text-warn">
          {device.presence.reason}
        </div>
      ) : null}

      <Tabs value={tab} onValueChange={(value) => setTab(String(value))}>
        <div className="-mx-1 overflow-x-auto px-1 scrollbar-none">
          <TabsList className="w-max">
            {TABS.map((item) => (
              <TabsTrigger key={item.value} value={item.value} className="min-h-9 px-3">
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
      </Tabs>
    </div>
  );
}