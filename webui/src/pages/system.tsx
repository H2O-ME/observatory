import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import {
  Archive,
  Database,
  HardDrive,
  Images,
  MonitorSmartphone,
  PackageCheck,
  Radio,
  RefreshCw,
  ServerCog,
  Timer,
} from "lucide-react";

import { BentoGridItem, GlowCard, GridPattern } from "@/components/aceternity";
import { ErrorBlock, LoadingBlock } from "@/components/async-state";
import { RingMeter } from "@/components/meter";
import { PageHeader } from "@/components/page-header";
import { StatCard } from "@/components/stat-card";
import { Button } from "@/components/ui/button";
import { useViewport } from "@/hooks/use-viewport";
import { api, isApiError } from "@/lib/api";
import { formatBytes, formatDateTime, formatDuration } from "@/lib/format";
import { useServerNow } from "@/store/clock";
import { cn } from "@/lib/utils";

const CARRIER_LABEL: Record<string, string> = {
  ws: "WebSocket",
  poll: "HTTP 轮询",
  http: "HTTP（旧链路）",
  none: "无载波",
};

/**
 * Server status.
 *
 * Everything on this page is a server number: `/api/v1/system` for the detailed
 * view, `/api/v1/health` for the public carrier counts, and the *global* settings
 * values for the retention policy (those are what the retention task actually
 * reads). The console computes nothing except a disk-used percentage.
 *
 * Known gap: the schema version is not exposed by any endpoint yet (`core/db.py`
 * has `SCHEMA_VERSION = 3`), so the page says so instead of inventing a value.
 *
 * Presentation (0.4): four telemetry tiles, a lit storage ring, then two-column
 * source cards; one column on phones/watches.
 */
export function SystemPage() {
  const now = useServerNow();
  const viewport = useViewport();
  const desktop = viewport === "desktop";
  const watch = viewport === "watch";
  const stack = watch ? "space-y-3" : "space-y-5";

  const systemQuery = useQuery({ queryKey: ["system"], queryFn: api.getSystem, refetchInterval: 15_000 });
  const healthQuery = useQuery({ queryKey: ["health"], queryFn: api.getHealth, refetchInterval: 15_000 });
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: api.getSettings, staleTime: 60_000 });

  const system = systemQuery.data;
  const health = healthQuery.data;
  const values = settingsQuery.data?.values ?? {};

  const carriers = health?.carriers ?? system?.channels ?? {};
  const carrierEntries = Object.entries(carriers).sort(([a], [b]) => a.localeCompare(b));
  const storage = system?.storage;
  const diskUsedPercent =
    storage && storage.disk_total > 0
      ? Math.round(((storage.disk_total - storage.disk_free) / storage.disk_total) * 100)
      : null;

  const num = (key: string): number | null => {
    const value = values[key];
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  };
  const text = (key: string): string => {
    const value = values[key];
    return typeof value === "string" ? value : "";
  };
  const bool = (key: string): boolean => Boolean(values[key]);
  const refreshing = systemQuery.isFetching || healthQuery.isFetching;

  return (
    <div className="w-full min-w-0">
      <PageHeader
        icon={ServerCog}
        title="系统"
        description="版本、载波、媒体用量与保留策略；全部来自服务端接口。"
        actions={
          <Button
            size="sm"
            variant="outline"
            className="tap"
            onClick={() => {
              void systemQuery.refetch();
              void healthQuery.refetch();
            }}
            disabled={refreshing}
          >
            <RefreshCw className={cn("size-3.5", refreshing && "animate-spin")} />
            刷新
          </Button>
        }
      />

      <div className={cn("mt-4 min-w-0 sm:mt-5", stack)}>
        {systemQuery.isError ? (
          <ErrorBlock
            message={isApiError(systemQuery.error) ? systemQuery.error.message : "无法读取系统状态"}
            retryable
            onRetry={() => void systemQuery.refetch()}
          />
        ) : systemQuery.isLoading ? (
          <LoadingBlock label="正在读取系统状态…" rows={5} />
        ) : null}

        {system ? (
          <>
            <section aria-label="服务端指标" className="grid min-w-0 grid-cols-2 gap-3 sm:gap-5 lg:grid-cols-4">
              <StatCard
                label="服务端版本"
                value={`v${system.version || "—"}`}
                hint="控制台服务端"
                icon={ServerCog}
                tone="brand"
              />
              <StatCard label="运行时长" value={formatDuration(system.uptime_s)} hint="自服务端启动" icon={Timer} />
              <StatCard
                label="设备"
                value={String(system.devices)}
                hint={`WS ${carriers.ws ?? "—"} · 轮询 ${carriers.poll ?? "—"}`}
                icon={MonitorSmartphone}
                tone="brand"
              />
              <StatCard
                label="画面"
                value={String(system.frames)}
                hint={formatBytes(storage?.frames_bytes)}
                icon={Images}
              />
            </section>

            {/* ------------------------------------------------------ storage hero */}
            <GlowCard isStatic={!desktop} className="min-w-0" contentClassName="p-3 sm:p-5">
              <div className="grid min-w-0 gap-4 md:grid-cols-[auto_minmax(0,1fr)] md:items-center md:gap-6">
                <div className="flex min-w-0 justify-center md:justify-start">
                  <RingMeter label="磁盘占用" value={diskUsedPercent} />
                </div>
                <div className="min-w-0">
                  <h2 className="flex min-w-0 items-center text-[0.98rem] font-semibold tracking-tight text-ink">
                    <span className="mr-2 flex size-7 shrink-0 items-center justify-center rounded-lg border border-line bg-sunken text-brand">
                      <Database className="size-3.5" />
                    </span>
                    数据量与存储
                  </h2>
                  <p className="mt-1 text-[0.75rem] leading-relaxed text-soft">
                    磁盘已用 <span className="tnum">{diskUsedPercent ?? "—"}%</span>（服务端计算）。
                  </p>
                  <dl className="mt-2.5 grid min-w-0 grid-cols-2 gap-1.5 sm:gap-2">
                    <StorageTile label="数据库" value={formatBytes(storage?.db_bytes)} />
                    <StorageTile label="画面文件" value={formatBytes(storage?.frames_bytes)} />
                    <StorageTile label="缩略图" value={formatBytes(storage?.thumbs_bytes)} />
                    <StorageTile
                      label="数据卷"
                      value={`${formatBytes(storage?.disk_free)} 可用 / ${formatBytes(storage?.disk_total)} 总容量`}
                    />
                  </dl>
                  {num("storage_quota_mb") !== null ? (
                    <p className="mt-2 text-[0.72rem] leading-relaxed text-faint">
                      数据卷配额 {formatBytes((num("storage_quota_mb") ?? 0) * 1024 * 1024)}，回收目标占用{" "}
                      {num("quota_target") ?? "—"}%。
                    </p>
                  ) : null}
                </div>
              </div>
            </GlowCard>

            {/* -------------------------------------------------------- source cards */}
            <div className="grid min-w-0 gap-4 sm:gap-5 lg:grid-cols-2">
              <BentoGridItem
                title="设备载波"
                description="载波 → 会话数（链路注册表快照）"
                icon={<Radio />}
              >
                {carrierEntries.length === 0 ? (
                  <div className="relative mt-2 min-w-0 overflow-hidden rounded-lg border border-dashed border-line bg-sunken px-2.5 py-3 text-center">
                    <GridPattern
                      className="pointer-events-none absolute inset-full opacity-30"
                      stroke="var(--c-grid-line)"
                      fade="radial"
                    />
                    <p className="relative text-[0.75rem] text-faint">当前没有任何受控端链路。</p>
                  </div>
                ) : (
                  <dl className="mt-2 grid min-w-0">
                    {carrierEntries.map(([carrier, count]) => (
                      <KvRow key={carrier} label={<span className="break-all">{carrier}{CARRIER_LABEL[carrier] ? ` · ${CARRIER_LABEL[carrier]}` : ""}</span>}>
                        <span className="tnum font-semibold text-ink">{count}</span>
                      </KvRow>
                    ))}
                  </dl>
                )}
              </BentoGridItem>

              <BentoGridItem title="运行与命令" description="服务端时钟与命令队列" icon={<Timer />}>
                <dl className="mt-2 grid min-w-0">
                  <KvRow label="服务端时间">
                    <span className="tnum break-all">{formatDateTime(now ?? system.server_time_ms)}</span>
                  </KvRow>
                  <KvRow label="命令记录">
                    <span className="tnum">{system.commands}</span>
                  </KvRow>
                  <KvRow label="待处理命令">
                    <span className={cn("tnum", system.pending_commands > 0 ? "font-semibold text-warn" : "text-soft")}>
                      {system.pending_commands}
                    </span>
                  </KvRow>
                  <KvRow label="控制会话">
                    <span className="tnum">{system.control_sessions}</span>
                  </KvRow>
                  <KvRow label="schema 版本">
                    <span className="text-[0.72rem] leading-relaxed text-warn">服务端未上报</span>
                  </KvRow>
                  <KvRow label="安装">
                    <span className="break-all">
                      {system.install.enabled ? `可用 · 待用令牌 ${system.install.available_tokens}` : "未启用"}
                    </span>
                  </KvRow>
                </dl>
              </BentoGridItem>

              <BentoGridItem title="保留策略" description="全局设置，服务端实际生效值" icon={<HardDrive />}>
                {settingsQuery.isError ? (
                  <p className="mt-2 text-[0.78rem] leading-relaxed text-warn">无法读取设置中的保留策略值。</p>
                ) : (
                  <dl className="mt-2 grid min-w-0">
                    <KvRow label="画面保留">
                      <span className="tnum">{num("frame_retention_days") ?? "—"} 天</span>
                    </KvRow>
                    <KvRow label="保留任务间隔">
                      <span className="tnum">{num("media_retention_interval_s") ?? "—"} 秒</span>
                    </KvRow>
                    <KvRow label="审计保留">
                      <span className="tnum">{num("audit_retention_days") ?? "—"} 天</span>
                    </KvRow>
                    <KvRow label="命令记录保留">
                      <span className="tnum">{num("command_retention_days") ?? "—"} 天</span>
                    </KvRow>
                  </dl>
                )}
              </BentoGridItem>

              <BentoGridItem title="归档配置" description="远端仓库与失败队列" icon={<Archive />}>
                <dl className="mt-2 grid min-w-0">
                  <KvRow label="归档开关">
                    <span className={bool("archive_enabled") ? "text-ok" : "text-soft"}>
                      {bool("archive_enabled") ? "已开启" : "已关闭"}
                    </span>
                  </KvRow>
                  <KvRow label="仓库">
                    <span className="break-all font-mono text-[0.74rem]">{text("archive_repo") || "—"}</span>
                  </KvRow>
                  <KvRow label="分支">
                    <span className="break-all font-mono text-[0.74rem]">{text("archive_branch") || "—"}</span>
                  </KvRow>
                  <KvRow label="路径前缀">
                    <span className="break-all font-mono text-[0.74rem]">{text("archive_path_prefix") || "—"}</span>
                  </KvRow>
                </dl>
              </BentoGridItem>

              <BentoGridItem title="安装与访问" description="控制台域名与安装脚本" icon={<PackageCheck />}>
                <dl className="mt-2 grid min-w-0">
                  <KvRow label="控制台域名">
                    <span className="break-all">{system.console_host || "—"}</span>
                  </KvRow>
                  <KvRow label="备用域名">
                    <span className="break-all">{system.base_urls.join("  |  ") || "—"}</span>
                  </KvRow>
                  <KvRow label="安装版本">
                    <span className="tnum">v{system.install.version || "—"}</span>
                  </KvRow>
                  <KvRow label="安装脚本">
                    <span className="break-all font-mono text-[0.7rem]">
                      {system.install.script_url || "—"}
                      <br />
                      {system.install.cmd_url || "—"}
                    </span>
                  </KvRow>
                </dl>
              </BentoGridItem>

            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}

/** One compact storage figure. */
function StorageTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-xl border border-line bg-sunken p-2">
      <dt className="truncate text-[0.66rem] font-medium uppercase tracking-[0.12em] text-faint">{label}</dt>
      <dd className="tnum mt-0.5 min-w-0 break-words text-[0.8rem] font-semibold text-ink">{value}</dd>
    </div>
  );
}

/** Definition-list row used by the bento cards (grid, never flex gap). */
function KvRow({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,6.5rem)_minmax(0,1fr)] items-baseline gap-2 border-b border-line py-1.5 last:border-b-0 last:pb-0">
      <dt className="min-w-0 break-all text-[0.7rem] text-faint">{label}</dt>
      <dd className="min-w-0 break-all text-[0.78rem] text-soft">{children}</dd>
    </div>
  );
}
