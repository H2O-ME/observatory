import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";

import { Timeline, TimelineItem } from "@/components/aceternity";
import { EmptyBlock, ErrorBlock, LoadingBlock, Pager } from "@/components/async-state";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useViewport } from "@/hooks/use-viewport";
import { api, isApiError } from "@/lib/api";
import { formatDate, formatTime } from "@/lib/format";
import { parseDateTimeLocal } from "@/lib/time-window";
import type { AuditEntry } from "@/lib/types";
import { cn } from "@/lib/utils";

const PAGE = 50;

/** Dotted action names from `console/audit.py`, labelled for the operator. */
const ACTION_LABEL: Record<string, string> = {
  "auth.login": "登录",
  "auth.login_failed": "登录失败",
  "auth.logout": "登出",
  "auth.totp_enable": "启用二次验证",
  "auth.totp_disable": "关闭二次验证",
  "auth.password_change": "修改密码",
  "settings.update": "修改设置",
  "device.update": "修改设备",
  "device.delete": "删除设备",
  "device.uninstall": "远程卸载",
  "device.enroll": "设备注册",
  "command.create": "创建命令",
  "control.grant": "远程控制",
  "files.op": "文件操作",
  "package.upload": "上传安装包",
  "package.delete": "删除安装包",
  "install.token_create": "生成安装令牌",
  "install.token_delete": "撤销安装令牌",
  "backup.retrieve": "取回归档",
  "backup.delete": "删除归档",
};

/**
 * Audit trail.
 *
 * The server filters by `action` and `device_uid` only (no actor/time params), so
 * those two are server-side and the actor/time boxes filter *the current page* —
 * the toolbar says so instead of pretending to search the whole trail. Every
 * timestamp is the server's `ts_ms`.
 */
export function AuditPage() {
  const viewport = useViewport();
  const watch = viewport === "watch";
  const desktop = viewport === "desktop";
  const touch = !desktop;
  const [action, setAction] = useState("");
  const [deviceUid, setDeviceUid] = useState("");
  const [actor, setActor] = useState("");
  const [timeFrom, setTimeFrom] = useState("");
  const [timeTo, setTimeTo] = useState("");
  const [offset, setOffset] = useState(0);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);

  const devicesQuery = useQuery({
    queryKey: ["devices"],
    queryFn: api.listDevices,
    staleTime: 60_000,
  });

  const query = useQuery({
    queryKey: ["audit", action, deviceUid, PAGE, offset],
    queryFn: () => api.listAudit({ limit: PAGE, offset, action, deviceUid }),
    refetchInterval: 30_000,
  });

  const entries = query.data?.entries ?? [];
  const fromMs = parseDateTimeLocal(timeFrom);
  const toMs = parseDateTimeLocal(timeTo);
  const visible = entries.filter((entry) => {
    if (actor && !entry.actor.toLowerCase().includes(actor.trim().toLowerCase())) return false;
    if (fromMs !== null && entry.ts_ms < fromMs) return false;
    if (toMs !== null && entry.ts_ms >= toMs) return false;
    return true;
  });
  const devices = devicesQuery.data?.devices ?? [];
  const hasActiveFilter = Boolean(action || deviceUid || actor || timeFrom || timeTo);

  /** Keep server order (newest first) while grouping consecutive days. */
  const groups: Array<{ day: string; items: AuditEntry[] }> = [];
  for (const entry of visible) {
    const day = formatDate(entry.ts_ms);
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.items.push(entry);
    else groups.push({ day, items: [entry] });
  }

  const changeFilter = (next: () => void) => {
    next();
    setOffset(0);
  };

  const clearFilters = () =>
    changeFilter(() => {
      setAction("");
      setDeviceUid("");
      setActor("");
      setTimeFrom("");
      setTimeTo("");
    });

  const fieldClass = cn("h-9 w-full text-xs", touch && "h-11");

  return (
    <div className={cn("w-full max-w-full min-w-0 space-y-5", watch && "space-y-3", desktop && "mx-auto max-w-[72rem]")}>
      <PageHeader
        title="审计日志"
        description="登录、设置、命令、文件与归档操作；时间均为服务器时间。"
        actions={
          <Button
            size="sm"
            variant="outline"
            className={cn("w-full sm:w-auto", touch && "min-h-11")}
            onClick={() => void query.refetch()}
            disabled={query.isFetching}
          >
            <RefreshCw className={cn("size-3.5", query.isFetching && "animate-spin")} aria-hidden />
            刷新
          </Button>
        }
      />

      <div className="surface grid min-w-0 gap-2 p-3">
        <div
          className={cn(
            "grid min-w-0 gap-2",
            desktop
              ? "grid-cols-[minmax(0,10rem)_minmax(0,12rem)_minmax(0,17rem)_auto] items-center"
              : "grid-cols-2",
          )}
        >
          <Select
            value={action || null}
            onValueChange={(value) => changeFilter(() => setAction(typeof value === "string" ? value : ""))}
          >
            <SelectTrigger size="sm" className={cn("w-full", touch && "min-h-11")} aria-label="按动作筛选">
              <SelectValue placeholder="全部动作" />
            </SelectTrigger>
            <SelectContent>
              {Object.entries(ACTION_LABEL).map(([value, label]) => (
                <SelectItem key={value} value={value}>
                  {label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select
            value={deviceUid || null}
            onValueChange={(value) => changeFilter(() => setDeviceUid(typeof value === "string" ? value : ""))}
          >
            <SelectTrigger size="sm" className={cn("w-full", touch && "min-h-11")} aria-label="按设备筛选">
              <SelectValue placeholder="全部设备" />
            </SelectTrigger>
            <SelectContent>
              {devices.map((device) => (
                <SelectItem key={device.uid} value={device.uid}>
                  {device.name || device.uid}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          {desktop ? (
            <Input
              value={actor}
              onChange={(event) => setActor(event.target.value)}
              placeholder="操作者（当前页内）"
              className={fieldClass}
              aria-label="操作者（当前页内筛选）"
            />
          ) : null}

          {desktop && hasActiveFilter ? (
            <Button size="sm" variant="ghost" onClick={clearFilters}>
              清除筛选
            </Button>
          ) : null}
        </div>

        {!desktop ? (
          <div className="flex min-w-0 flex-wrap items-center">
            <Button
              size="sm"
              variant="outline"
              className="min-h-11"
              onClick={() => setFiltersOpen((open) => !open)}
              aria-expanded={filtersOpen}
            >
              {filtersOpen ? "收起筛选" : "更多筛选"}
            </Button>
            {hasActiveFilter ? (
              <Button size="sm" variant="ghost" className="ml-2 min-h-11" onClick={clearFilters}>
                清除筛选
              </Button>
            ) : null}
          </div>
        ) : null}

        {desktop ? (
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <Input
              type="datetime-local"
              value={timeFrom}
              onChange={(event) => setTimeFrom(event.target.value)}
              className={cn(fieldClass, "w-[15.5rem]")}
              aria-label="起始时间（当前页内筛选）"
              title="起始时间（当前页内筛选）"
            />
            <span className="text-[0.75rem] text-faint">至</span>
            <Input
              type="datetime-local"
              value={timeTo}
              onChange={(event) => setTimeTo(event.target.value)}
              className={cn(fieldClass, "w-[15.5rem]")}
              aria-label="结束时间（当前页内筛选）"
              title="结束时间（当前页内筛选）"
            />
          </div>
        ) : filtersOpen ? (
          <div className="grid min-w-0 grid-cols-1 gap-2">
            <Input
              value={actor}
              onChange={(event) => setActor(event.target.value)}
              placeholder="操作者（当前页内）"
              className={fieldClass}
              aria-label="操作者（当前页内筛选）"
            />
            <Input
              type="datetime-local"
              value={timeFrom}
              onChange={(event) => setTimeFrom(event.target.value)}
              className={fieldClass}
              aria-label="起始时间（当前页内筛选）"
              title="起始时间（当前页内筛选）"
            />
            <Input
              type="datetime-local"
              value={timeTo}
              onChange={(event) => setTimeTo(event.target.value)}
              className={fieldClass}
              aria-label="结束时间（当前页内筛选）"
              title="结束时间（当前页内筛选）"
            />
          </div>
        ) : null}

        <p className="min-w-0 text-[0.7rem] leading-relaxed text-faint">
          动作与设备由服务端筛选；操作者与时间只作用于当前页。
        </p>
      </div>

      {query.isError ? (
        <ErrorBlock
          message={isApiError(query.error) ? query.error.message : "无法读取审计日志"}
          retryable
          onRetry={() => void query.refetch()}
        />
      ) : query.isLoading ? (
        <LoadingBlock label="正在读取审计日志…" rows={5} />
      ) : visible.length === 0 ? (
        <EmptyBlock
          title={entries.length === 0 ? "没有审计记录" : "当前页内没有匹配的操作者"}
          description={
            entries.length === 0
              ? "筛选条件越少越容易命中；审计从首次启动开始记录。"
              : "操作者与时间范围只作用于当前页；可以翻页或放宽条件。"
          }
        />
      ) : (
        <div className="grid min-w-0 gap-4 sm:gap-5">
          {groups.map((group) => (
            <div key={group.day} className="surface min-w-0 p-3.5 sm:p-4">
              <h2 className="tnum min-w-0 truncate text-[0.85rem] font-semibold text-ink">{group.day}</h2>
              <Timeline className="mt-3">
                {group.items.map((entry) => (
                  <AuditTimelineItem
                    key={entry.id}
                    entry={entry}
                    open={expanded === entry.id}
                    touch={touch}
                    onToggle={() => setExpanded(expanded === entry.id ? null : entry.id)}
                  />
                ))}
              </Timeline>
            </div>
          ))}
        </div>
      )}

      <Pager total={query.data?.total ?? 0} limit={PAGE} offset={offset} onChange={setOffset} busy={query.isFetching} />
    </div>
  );
}

function AuditTimelineItem({
  entry,
  open,
  touch,
  onToggle,
}: {
  entry: AuditEntry;
  open: boolean;
  touch: boolean;
  onToggle: () => void;
}) {
  const hasDetail = Object.keys(entry.detail ?? {}).length > 0;
  const device = entry.device_name || entry.device_uid;
  return (
    <TimelineItem
      date={formatTime(entry.ts_ms)}
      title={<span className="min-w-0 break-words">{ACTION_LABEL[entry.action] ?? entry.action}</span>}
      description={
        <span className="block min-w-0">
          <span className="mt-0.5 block min-w-0 break-words">
            {entry.actor || "console"}
            {device ? ` · ${device}` : ""}
            {entry.ip ? ` · ${entry.ip}` : ""}
          </span>
          <span className="mt-0.5 block min-w-0 break-all font-mono text-[0.68rem] text-faint">{entry.action}</span>
        </span>
      }
    >
      {hasDetail ? (
        <div className="grid min-w-0 gap-1.5">
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={open}
            className={cn(
              "inline-flex w-fit min-w-0 items-center rounded-lg border border-line bg-sunken px-2.5 py-1 text-[0.72rem] font-medium text-soft transition-colors hover:text-ink",
              touch && "min-h-11",
            )}
          >
            {open ? (
              <ChevronDown className="mr-1 size-3.5 shrink-0" aria-hidden />
            ) : (
              <ChevronRight className="mr-1 size-3.5 shrink-0" aria-hidden />
            )}
            {open ? "收起 JSON" : "查看 JSON"}
          </button>
          {open ? (
            <pre className="hide-scrollbar max-h-56 w-full max-w-full overflow-y-auto overflow-x-hidden whitespace-pre-wrap break-words rounded-lg border border-line bg-sunken p-2 font-mono text-[0.7rem] leading-relaxed text-soft">
              {JSON.stringify(entry.detail, null, 2)}
            </pre>
          ) : null}
        </div>
      ) : null}
    </TimelineItem>
  );
}
