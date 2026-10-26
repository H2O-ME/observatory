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
 * Device shell: identity hero, telemetry strip, then a scrollable tab strip.
 *
 * The active tab lives in the URL (`/device/:uid/<tab>`), so a link to the live
 * view stays a link to the live view after a reload. The 0.2.x URLs (`/` and
 * `/devices/:uid`) are served by the same component.
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
          className="inline-flex min-h-11 items-center text-xs text-muted-foreground no-underline hover:text-foreground"
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
        <div className="flex items-start gap-3">
          <Skeleton className="size-8 shrink-0 rounded-md" />
          <div className="space-y-2">
            <Skeleton className="h-7 w-56" />
            <Skeleton className="h-3 w-72" />
          </div>
        </div>
        <Skeleton className="h-[74px] w-full rounded-md" />
        <Skeleton className="h-64 w-full rounded-md" />
      </div>
    );
  }

  return (
    <div className={cn("min-w-0", spacing)}>
      <DeviceHero device={device} isFetching={query.isFetching} onRefresh={() => void query.refetch()} />

      <Tabs value={tab} onValueChange={selectTab} className="flex min-w-0 flex-col gap-0">
        <div className="scroll-x hide-scrollbar mb-6 w-full max-w-full border-b border-border">
          <TabsList variant="line" className="h-9 w-full justify-start border-0">
            {TABS.map((item) => (
              <TabsTrigger key={item.value} value={item.value} className="h-full flex-none px-3 py-2">
                {item.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>

        <TabsContent value="overview">
          <OverviewView device={device} onRefresh={() => void query.refetch()} />
        </TabsContent>
        {/* keepMounted: switching tabs used to unmount these views — a live
            control session was silently revoked, a file transfer aborted, a
            journal page position lost. Cheap views (overview/timeline/settings)
            still unmount to stop their timers. */}
        <TabsContent value="live" keepMounted>
          <LiveView device={device} />
        </TabsContent>
        <TabsContent value="timeline">
          <TimelineView device={device} />
        </TabsContent>
        <TabsContent value="files" keepMounted>
          <FilesView device={device} />
        </TabsContent>
        <TabsContent value="commands" keepMounted>
          <CommandsView device={device} />
        </TabsContent>
        <TabsContent value="settings">
          <DeviceSettingsView device={device} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
