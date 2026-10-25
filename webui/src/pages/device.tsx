import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ChevronLeft } from "lucide-react";

import { ErrorBlock } from "@/components/async-state";
import { DeviceHero } from "@/pages/device/device-hero";
import { CommandsView } from "@/pages/device/commands-view";
import { FilesView } from "@/pages/device/files-view";
import { LiveView } from "@/pages/device/live-view";
import { OverviewView } from "@/pages/device/overview-view";
import { DeviceSettingsView } from "@/pages/device/settings-view";
import { TimelineView } from "@/pages/device/timeline-view";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, isApiError } from "@/lib/api";
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
 * Device shell（0.4 重写）：mission-control hero + 遥测条 + 自滚动的 tab 条。
 *
 * The active tab lives in the URL (`/device/:uid/<tab>`), so a link to the live
 * view stays a link to the live view after a reload. The 0.2.x URLs (`/` and
 * `/devices/:uid`) are served by the same component. The tab strip scrolls
 * inside itself only — never the page — and the trigger heights are driven
 * inline because the shared Tabs primitive pins a 32px list height.
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
  const desktop = viewport === "desktop";

  const query = useQuery({
    queryKey: ["device", uid],
    queryFn: () => api.getDevice(uid),
    enabled: Boolean(uid),
    refetchInterval: connected ? 10_000 : 4_000,
  });

  const device = query.data?.device;
  const spacing = watch ? "space-y-2" : "space-y-3 sm:space-y-4";

  const selectTab = (value: string) => {
    const next: TabValue = isTabValue(value) ? value : "overview";
    navigate(`/device/${encodeURIComponent(uid)}/${next}`);
  };

  if (query.isError) {
    return (
      <div className="min-w-0 space-y-3">
        <Link
          to="/wall"
          className="inline-flex min-h-11 items-center text-[0.8rem] text-soft no-underline"
        >
          <ChevronLeft className="mr-1 size-4" aria-hidden />
          返回设备墙
        </Link>
        <ErrorBlock
          message={`无法读取设备：${isApiError(query.error) ? query.error.message : "未知错误"}`}
        />
      </div>
    );
  }

  if (!device) {
    return (
      <div className={cn("min-w-0", spacing)}>
        <div className="surface min-w-0 animate-pulse" style={{ height: watch ? 120 : 148 }} />
        <div className="grid grid-cols-2 gap-2 sm:gap-3 md:grid-cols-4">
          {[0, 1, 2, 3].map((index) => (
            <Skeleton key={index} className="h-[76px] w-full rounded-panel" />
          ))}
        </div>
        <Skeleton className="h-64 w-full rounded-panel" />
      </div>
    );
  }

  return (
    <div className={cn("min-w-0", spacing)}>
      <DeviceHero device={device} isFetching={query.isFetching} onRefresh={() => void query.refetch()} />


      <Tabs value={tab} onValueChange={selectTab} className="flex min-w-0 flex-col">
        <div className="surface min-w-0 p-1">
          <div className="scroll-x hide-scrollbar w-full max-w-full">
            <TabsList
              className="items-center"
              style={{
                height: "auto",
                width: "max-content",
                padding: 0,
                backgroundColor: "transparent",
                borderRadius: 999,
              }}
            >
              {TABS.map((item) => {
                const active = tab === item.value;
                return (
                  <TabsTrigger
                    key={item.value}
                    value={item.value}
                    className={cn(
                      "relative min-w-0 whitespace-nowrap border border-transparent font-medium transition-colors",
                      active ? "text-brand-ink" : "text-soft hover:text-ink",
                    )}
                    style={{
                      height: desktop ? 36 : 40,
                      paddingLeft: desktop ? 18 : 13,
                      paddingRight: desktop ? 18 : 13,
                      borderRadius: 999,
                      fontSize: watch ? "0.78rem" : "0.84rem",
                      ...(active
                        ? {
                            backgroundColor: "var(--c-brand)",
                            borderColor: "var(--c-brand)",
                            color: "var(--c-brand-ink)",
                            boxShadow: "var(--sh-card)",
                          }
                        : { color: "var(--c-soft)" }),
                    }}
                  >
                    {item.label}
                  </TabsTrigger>
                );
              })}
            </TabsList>
          </div>
        </div>

        <TabsContent value="overview" className={watch ? "mt-2" : "mt-3 sm:mt-4"}>
          <OverviewView device={device} onRefresh={() => void query.refetch()} />
        </TabsContent>
        <TabsContent value="live" className={watch ? "mt-2" : "mt-3 sm:mt-4"}>
          <LiveView device={device} />
        </TabsContent>
        <TabsContent value="timeline" className={watch ? "mt-2" : "mt-3 sm:mt-4"}>
          <TimelineView device={device} />
        </TabsContent>
        <TabsContent value="files" className={watch ? "mt-2" : "mt-3 sm:mt-4"}>
          <FilesView device={device} />
        </TabsContent>
        <TabsContent value="commands" className={watch ? "mt-2" : "mt-3 sm:mt-4"}>
          <CommandsView device={device} />
        </TabsContent>
        <TabsContent value="settings" className={watch ? "mt-2" : "mt-3 sm:mt-4"}>
          <DeviceSettingsView device={device} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
