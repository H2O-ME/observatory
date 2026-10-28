import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { RefreshCw } from "lucide-react";

import { ErrorBlock, LoadingBlock } from "@/components/async-state";
import { RingMeter } from "@/components/meter";
import { PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
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

const CARRIER_DOT: Record<string, string> = {
  ws: "bg-success",
  poll: "bg-warning",
  http: "bg-warning",
  none: "bg-faint",
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
 */
export function SystemPage() {
  const now = useServerNow();
  const viewport = useViewport();
  const watch = viewport === "watch";

  const systemQuery = useQuery({ queryKey: ["system"], queryFn: api.getSystem, refetchInterval: 15_000 });
  const healthQuery = useQuery({ queryKey: ["health"], queryFn: api.getHealth, refetchInterval: 15_000 });
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: api.getSettings, staleTime: 60_000 });

  const system = systemQuery.data;
  const health = healthQuery.data;
  const values = settingsQuery.data?.values ?? {};

  const carriers = health?.carriers ?? system?.channels ?? {};
  const carrierEntries = Object.entries(carriers).sort(([a], [b]) => a.localeCompare(b));
  const carrierTotal = carrierEntries.reduce(
    (sum, [, count]) => sum + (typeof count === "number" ? count : 0),
    0,
  );
  const storage = system?.storage;
  const diskUsedBytes = storage ? Math.max(0, storage.disk_total - storage.disk_free) : null;
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

  const quotaMb = num("storage_quota_mb");
  const quotaTarget = num("quota_target");

  return (
    <div className="w-full min-w-0">
      <PageHeader
        title="系统"
        description="版本、载波、媒体用量与保留策略；全部来自服务端接口。"
        actions={
          <Button
            size="sm"
            variant="outline"
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

      <div className={cn("mt-6 min-w-0 grid gap-6", watch && "mt-4 gap-3")}>
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
            {/* -------------------------------------------------- headline metrics */}
            <SysCard>
              <div className="grid grid-cols-2 gap-x-6 gap-y-6 sm:grid-cols-4 sm:gap-x-0">
                <Metric label="服务端版本" value={`v${system.version || "—"}`} hint="控制台服务端" />
                <Metric label="运行时长" value={formatDuration(system.uptime_s)} hint="自服务端启动" />
                <Metric
                  label="设备"
                  value={String(system.devices)}
                  hint={`WS ${carriers.ws ?? "—"} · 轮询 ${carriers.poll ?? "—"}`}
                />
                <Metric label="画面" value={String(system.frames)} hint={formatBytes(storage?.frames_bytes)} />
              </div>
            </SysCard>

            {/* -------------------------------------------------------- storage */}
            <SysCard title="数据量与存储" hint="全部为服务端统计值">
              <div className="flex min-w-0 flex-col gap-8 md:flex-row md:items-center md:gap-12">
                <div className="flex shrink-0 justify-center md:justify-start">
                  <RingMeter
                    label="磁盘占用"
                    value={diskUsedPercent}
                    size={124}
                    stroke={10}
                    caption={
                      storage
                        ? `${formatBytes(diskUsedBytes)} / ${formatBytes(storage.disk_total)}`
                        : "未上报"
                    }
                  />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-sm leading-relaxed text-muted-foreground">
                    磁盘已用{" "}
                    <span className="tnum font-medium text-foreground">{diskUsedPercent ?? "—"}%</span>
                    （服务端计算）。
                  </p>
                  <p className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1.5 text-sm text-muted-foreground">
                    <span>数据卷配额</span>
                    {quotaMb === null ? (
                      <Badge variant="secondary" className="text-foreground">
                        未设置
                      </Badge>
                    ) : (
                      <span className="tnum text-foreground">{formatBytes(quotaMb * 1024 * 1024)}</span>
                    )}
                    <span className="text-faint">·</span>
                    <span>回收目标</span>
                    {quotaTarget === null ? (
                      <Badge variant="secondary" className="text-foreground">
                        未设置
                      </Badge>
                    ) : (
                      <span className="tnum text-foreground">{quotaTarget}%</span>
                    )}
                  </p>
                  <dl className="mt-6 grid min-w-0 grid-cols-2 gap-x-8 gap-y-6 sm:grid-cols-4">
                    <StorageStat label="数据库" value={formatBytes(storage?.db_bytes)} />
                    <StorageStat label="画面文件" value={formatBytes(storage?.frames_bytes)} />
                    <StorageStat label="缩略图" value={formatBytes(storage?.thumbs_bytes)} />
                    <StorageStat label="数据卷可用" value={formatBytes(storage?.disk_free)} />
                  </dl>
                </div>
              </div>
            </SysCard>

            {/* --------------------------------------- carriers + retention pair */}
            <div className="grid min-w-0 gap-6 lg:grid-cols-2">
              <SysCard title="链路与保留" hint="载波会话数（链路注册表快照）与全局保留策略">
                <div className="grid min-w-0 gap-8 sm:grid-cols-2 sm:gap-10">
                  <section className="min-w-0">
                    <h3 className="text-sm font-semibold text-foreground">载波</h3>
                    {carrierEntries.length === 0 ? (
                      <p className="mt-3 text-sm text-muted-foreground">当前没有任何受控端链路。</p>
                    ) : (
                      <ul className="mt-1">
                        {carrierEntries.map(([carrier, count]) => (
                          <li
                            key={carrier}
                            className="grid grid-cols-[0.75rem_7rem_auto] items-center gap-x-2 py-2 text-sm"
                          >
                            <span
                              className={cn(
                                "size-1.5 shrink-0 rounded-full",
                                CARRIER_DOT[carrier] ?? "bg-faint",
                              )}
                              aria-hidden
                            />
                            <span className="truncate text-muted-foreground">
                              {CARRIER_LABEL[carrier] ?? carrier}
                            </span>
                            <span className="whitespace-nowrap">
                              <span className="text-faint">:</span>
                              <span className="tnum ml-1 font-medium text-foreground">{count}</span>
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                    {carrierEntries.length > 0 ? (
                      <p className="grid grid-cols-[0.75rem_7rem_auto] items-center gap-x-2 py-2 text-sm">
                        <span aria-hidden />
                        <span className="text-muted-foreground">合计</span>
                        <span className="whitespace-nowrap">
                          <span className="text-faint">:</span>
                          <span className="tnum ml-1 font-medium text-foreground">{carrierTotal}</span>
                        </span>
                      </p>
                    ) : null}
                  </section>

                  <section className="min-w-0">
                    <h3 className="text-sm font-semibold text-foreground">数据保留</h3>
                    {settingsQuery.isError ? (
                      <p className="mt-3 text-sm leading-relaxed text-warning">无法读取设置中的保留策略值。</p>
                    ) : (
                      <dl className="mt-1">
                        <Kv label="画面保留">
                          <span className="tnum text-foreground">{num("frame_retention_days") ?? "—"} 天</span>
                        </Kv>
                        <Kv label="保留任务间隔">
                          <span className="tnum text-foreground">{num("media_retention_interval_s") ?? "—"} 秒</span>
                        </Kv>
                        <Kv label="审计保留">
                          <span className="tnum text-foreground">{num("audit_retention_days") ?? "—"} 天</span>
                        </Kv>
                        <Kv label="命令记录保留">
                          <span className="tnum text-foreground">{num("command_retention_days") ?? "—"} 天</span>
                        </Kv>
                      </dl>
                    )}
                  </section>
                </div>
              </SysCard>

              <SysCard title="运行与命令" hint="服务端时钟与命令队列">
                <dl className="min-w-0">
                  <Kv label="服务端时间">
                    <span className="tnum break-all text-foreground">{formatDateTime(now ?? system.server_time_ms)}</span>
                  </Kv>
                  <Kv label="命令记录">
                    <span className="tnum font-medium text-foreground">{system.commands}</span>
                  </Kv>
                  <Kv label="待处理命令">
                    <span
                      className={cn(
                        "tnum",
                        system.pending_commands > 0 ? "font-medium text-warning" : "text-muted-foreground",
                      )}
                    >
                      {system.pending_commands}
                    </span>
                  </Kv>
                  <Kv label="控制会话">
                    <span className="tnum text-foreground">{system.control_sessions}</span>
                  </Kv>
                  <Kv label="schema 版本">
                    <Badge variant="warning">未上报</Badge>
                  </Kv>
                  <Kv label="安装">
                    <span className="break-all">
                      {system.install.enabled ? "可用 · 开放注册（无需令牌）" : "未启用"}
                    </span>
                  </Kv>
                </dl>
              </SysCard>
            </div>

            {/* ------------------------------------------ archive + install pair */}
            <div className="grid min-w-0 gap-6 lg:grid-cols-2">
              <SysCard title="归档配置" hint="远端仓库与失败队列">
                <dl className="min-w-0">
                  <Kv label="归档开关">
                    <span className={bool("archive_enabled") ? "text-success" : "text-muted-foreground"}>
                      {bool("archive_enabled") ? "已开启" : "已关闭"}
                    </span>
                  </Kv>
                  <Kv label="仓库">
                    <span className="break-all font-mono text-xs">{text("archive_repo") || "—"}</span>
                  </Kv>
                  <Kv label="分支">
                    <span className="break-all font-mono text-xs">{text("archive_branch") || "—"}</span>
                  </Kv>
                  <Kv label="路径前缀">
                    <span className="break-all font-mono text-xs">{text("archive_path_prefix") || "—"}</span>
                  </Kv>
                </dl>
              </SysCard>

              <SysCard title="安装与访问" hint="控制台域名与安装脚本">
                <dl className="min-w-0">
                  <Kv label="控制台域名">
                    <span className="break-all">{system.console_host || "—"}</span>
                  </Kv>
                  <Kv label="备用域名">
                    <span className="break-all">{system.base_urls.join("  |  ") || "—"}</span>
                  </Kv>
                  <Kv label="安装版本">
                    <span className="tnum text-foreground">v{system.install.version || "—"}</span>
                  </Kv>
                  <Kv label="安装脚本">
                    <span className="break-all font-mono text-xs">
                      {system.install.script_url || "—"}
                      <br />
                      {system.install.cmd_url || "—"}
                    </span>
                  </Kv>
                </dl>
              </SysCard>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}

/**
 * Page-local card: one flat surface with `shadow-sm`, no outer border and a
 * uniform p-6. The system page deliberately uses this instead of the bordered
 * Panel so the information, not the frame, carries the hierarchy.
 */
function SysCard({
  title,
  hint,
  children,
  className,
}: {
  title?: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("min-w-0 rounded-2xl bg-card p-6 shadow-sm", className)}>
      {title ? (
        <header className="mb-5 min-w-0">
          <h2 className="text-base font-semibold tracking-[-0.01em] text-foreground">{title}</h2>
          {hint ? <p className="mt-1 text-sm text-muted-foreground">{hint}</p> : null}
        </header>
      ) : null}
      {children}
    </section>
  );
}

function Metric({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="min-w-0 sm:px-6 sm:first:pl-0 sm:last:pr-0">
      <p className="truncate text-sm text-muted-foreground">{label}</p>
      <p className="tnum mt-2 truncate text-2xl leading-none font-bold tracking-[-0.025em] text-foreground">
        {value}
      </p>
      {hint ? <p className="mt-1.5 truncate text-sm text-muted-foreground">{hint}</p> : null}
    </div>
  );
}

function StorageStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 text-left">
      <dt className="truncate text-sm text-muted-foreground">{label}</dt>
      <dd className="tnum mt-1.5 min-w-0 break-words text-base font-semibold text-foreground">{value}</dd>
    </div>
  );
}

/** One right-aligned label/value line; no divider, breathing room via py-2. */
function Kv({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div className="flex min-w-0 items-center justify-between gap-4 py-2.5 text-sm leading-relaxed">
      <dt className="shrink-0 text-foreground">{label}</dt>
      <dd className="min-w-0 text-right text-muted-foreground">{children}</dd>
    </div>
  );
}
