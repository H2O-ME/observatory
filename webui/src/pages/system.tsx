import { useQuery } from "@tanstack/react-query";
import { Archive, Database, HardDrive, PackageCheck, Radio, RefreshCw, ServerCog, Timer } from "lucide-react";

import { Banner, ErrorBlock, LoadingBlock, PageHeader } from "@/components/async-state";
import { Meter } from "@/components/meter";
import { Button } from "@/components/ui/button";
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
 */
export function SystemPage() {
  const now = useServerNow();
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

  return (
    <div className="space-y-3">
      <PageHeader
        title="系统"
        subtitle="服务端详细状态：版本、载波、数据量、媒体用量与保留策略"
        actions={
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              void systemQuery.refetch();
              void healthQuery.refetch();
            }}
            disabled={systemQuery.isFetching || healthQuery.isFetching}
          >
            <RefreshCw className={cn("size-3.5", (systemQuery.isFetching || healthQuery.isFetching) && "animate-spin")} />
            刷新
          </Button>
        }
      />

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
          <section className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
            {[
              { label: "服务端版本", value: `v${system.version || "—"}` },
              { label: "运行时长", value: formatDuration(system.uptime_s) },
              { label: "设备", value: String(system.devices) },
              { label: "画面", value: String(system.frames) },
              { label: "命令记录", value: String(system.commands) },
              { label: "控制会话", value: String(system.control_sessions) },
            ].map((item) => (
              <div key={item.label} className="rounded-panel border border-line bg-panel p-2.5">
                <p className="text-[0.66rem] text-faint">{item.label}</p>
                <p className="mt-0.5 truncate font-mono text-sm font-semibold text-ink">{item.value}</p>
              </div>
            ))}
          </section>

          <div className="grid gap-3 lg:grid-cols-2">
            <section className="rounded-panel border border-line bg-panel p-3">
              <h2 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-soft">
                <Radio className="size-3.5" />
                设备载波
              </h2>
              {carrierEntries.length === 0 ? (
                <p className="text-[0.72rem] text-faint">当前没有任何受控端链路。</p>
              ) : (
                <ul className="space-y-1 text-[0.72rem]">
                  {carrierEntries.map(([carrier, count]) => (
                    <li key={carrier} className="flex items-center justify-between gap-2">
                      <span className="text-soft">
                        <span className="font-mono">{carrier}</span>
                        {CARRIER_LABEL[carrier] ? ` · ${CARRIER_LABEL[carrier]}` : ""}
                      </span>
                      <span className="font-mono text-ink">{count}</span>
                    </li>
                  ))}
                </ul>
              )}
              <p className="mt-1.5 text-[0.64rem] leading-relaxed text-faint">
                来自服务端链路注册表（与 <span className="font-mono">/api/v1/health</span> 同源）；控制台连接数不在此列。
              </p>
            </section>

            <section className="rounded-panel border border-line bg-panel p-3">
              <h2 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-soft">
                <Timer className="size-3.5" />
                运行与命令
              </h2>
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[0.72rem]">
                <dt className="text-faint">服务端当前时间</dt>
                <dd className="text-soft">{formatDateTime(now ?? system.server_time_ms)}</dd>
                <dt className="text-faint">待处理命令</dt>
                <dd className="text-soft">{system.pending_commands}</dd>
                <dt className="text-faint">控制会话</dt>
                <dd className="text-soft">{system.control_sessions}</dd>
                <dt className="text-faint">schema 版本</dt>
                <dd className="text-warn">服务端未上报（建议 /system 补充 SCHEMA_VERSION）</dd>
                <dt className="text-faint">安装</dt>
                <dd className="text-soft">
                  {system.install.enabled ? `可用 · 待用令牌 ${system.install.available_tokens}` : "未启用"}
                </dd>
              </dl>
            </section>
          </div>

          <section className="rounded-panel border border-line bg-panel p-3">
            <h2 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-soft">
              <Database className="size-3.5" />
              数据量与存储
            </h2>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[0.72rem]">
              <dt className="text-faint">数据库</dt>
              <dd className="text-soft">{formatBytes(storage?.db_bytes)}</dd>
              <dt className="text-faint">画面文件</dt>
              <dd className="text-soft">{formatBytes(storage?.frames_bytes)}</dd>
              <dt className="text-faint">缩略图</dt>
              <dd className="text-soft">{formatBytes(storage?.thumbs_bytes)}</dd>
              <dt className="text-faint">数据卷</dt>
              <dd className="text-soft">
                {formatBytes(storage?.disk_free)} 可用 / {formatBytes(storage?.disk_total)} 总容量
              </dd>
            </dl>
            <div className="mt-2">
              <Meter label={`磁盘占用（服务端计算 ${diskUsedPercent ?? "—"}%）`} value={diskUsedPercent} />
            </div>
            {num("storage_quota_mb") !== null ? (
              <p className="mt-1.5 text-[0.66rem] leading-relaxed text-faint">
                数据卷配额 {formatBytes((num("storage_quota_mb") ?? 0) * 1024 * 1024)}，回收目标占用{" "}
                {num("quota_target") ?? "—"}%。
              </p>
            ) : null}
          </section>

          <section className="rounded-panel border border-line bg-panel p-3">
            <h2 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-soft">
              <HardDrive className="size-3.5" />
              保留策略（全局设置，服务端实际生效值）
            </h2>
            {settingsQuery.isError ? (
              <p className="text-[0.72rem] text-warn">无法读取设置中的保留策略值。</p>
            ) : (
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[0.72rem]">
                <dt className="text-faint">画面保留</dt>
                <dd className="text-soft">{num("frame_retention_days") ?? "—"} 天</dd>
                <dt className="text-faint">保留任务间隔</dt>
                <dd className="text-soft">{num("media_retention_interval_s") ?? "—"} 秒</dd>
                <dt className="text-faint">审计保留</dt>
                <dd className="text-soft">{num("audit_retention_days") ?? "—"} 天</dd>
                <dt className="text-faint">命令记录保留</dt>
                <dd className="text-soft">{num("command_retention_days") ?? "—"} 天</dd>
              </dl>
            )}
            <p className="mt-1.5 text-[0.66rem] leading-relaxed text-faint">
              这里的值来自全局设置；设备级覆盖（设备页“设置”页签）不会改变保留策略在服务器上的执行值。
            </p>
          </section>

          <div className="grid gap-3 lg:grid-cols-2">
            <section className="rounded-panel border border-line bg-panel p-3">
              <h2 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-soft">
                <Archive className="size-3.5" />
                归档配置
              </h2>
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[0.72rem]">
                <dt className="text-faint">归档开关</dt>
                <dd className="text-soft">{bool("archive_enabled") ? "已开启" : "已关闭"}</dd>
                <dt className="text-faint">仓库</dt>
                <dd className="break-all text-soft">{text("archive_repo") || "—"}</dd>
                <dt className="text-faint">分支</dt>
                <dd className="text-soft">{text("archive_branch") || "—"}</dd>
                <dt className="text-faint">路径前缀</dt>
                <dd className="text-soft">{text("archive_path_prefix") || "—"}</dd>
              </dl>
              <p className="mt-1.5 text-[0.64rem] leading-relaxed text-faint">
                队列与失败明细见「备份」页；归档令牌（secret）不会出现在浏览器。
              </p>
            </section>

            <section className="rounded-panel border border-line bg-panel p-3">
              <h2 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-soft">
                <PackageCheck className="size-3.5" />
                安装与访问
              </h2>
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[0.72rem]">
                <dt className="text-faint">控制台域名</dt>
                <dd className="break-all text-soft">{system.console_host || "—"}</dd>
                <dt className="text-faint">备用域名</dt>
                <dd className="break-all text-soft">{system.base_urls.join("  |  ") || "—"}</dd>
                <dt className="text-faint">安装版本</dt>
                <dd className="font-mono text-soft">v{system.install.version || "—"}</dd>
                <dt className="text-faint">安装脚本</dt>
                <dd className="break-all font-mono text-[0.64rem] text-soft">
                  {system.install.script_url || "—"} · {system.install.cmd_url || "—"}
                </dd>
              </dl>
            </section>
          </div>

          <section className="rounded-panel border border-line bg-panel p-3">
            <h2 className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-soft">
              <ServerCog className="size-3.5" />
              页面说明
            </h2>
            <ul className="list-disc space-y-0.5 pl-4 text-[0.68rem] leading-relaxed text-soft">
              <li>所有数值来自服务端接口；控制台不推算“在线/新鲜度”，也不使用浏览器时钟判断时间。</li>
              <li>载波计数来自链路注册表快照，页面每 15 秒刷新一次。</li>
              <li>保留策略是全局值；按设备的覆盖不会出现在这里（见设备页“设置”页签）。</li>
            </ul>
          </section>
        </>
      ) : null}

      <Banner tone="info">
        待补字段：schema 版本（服务端已有 <span className="font-mono">core/db.py::SCHEMA_VERSION</span>，但
        <span className="font-mono"> /api/v1/system</span> 未输出）。补上后本页直接显示，无需前端再改结构。
      </Banner>
    </div>
  );
}