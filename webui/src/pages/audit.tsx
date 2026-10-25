import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, RefreshCw } from "lucide-react";

import { Banner, EmptyBlock, ErrorBlock, LoadingBlock, Pager } from "@/components/async-state";
import { PageShell } from "@/components/page-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api, isApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
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
 * those two are server-side and the actor box filters *the current page* — it is
 * labelled as such instead of pretending to search the whole trail. Every
 * timestamp is the server's `ts_ms`.
 */
export function AuditPage() {
  const [action, setAction] = useState("");
  const [deviceUid, setDeviceUid] = useState("");
  const [actor, setActor] = useState("");
  const [timeFrom, setTimeFrom] = useState("");
  const [timeTo, setTimeTo] = useState("");
  const [offset, setOffset] = useState(0);
  const [expanded, setExpanded] = useState<number | null>(null);

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
  const pageFilter = Boolean(actor || timeFrom || timeTo);
  const devices = devicesQuery.data?.devices ?? [];

  const changeFilter = (next: () => void) => {
    next();
    setOffset(0);
  };

  return (
    <PageShell
      title="审计日志"
      subtitle="所有登录、设置、命令、文件与归档操作；时间均为服务器时间"
      actions={
        <Button size="sm" variant="outline" onClick={() => void query.refetch()} disabled={query.isFetching}>
          <RefreshCw className={cn("size-3.5", query.isFetching && "animate-spin")} />
          刷新
        </Button>
      }
    >

      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={action || null}
          onValueChange={(value) => changeFilter(() => setAction(typeof value === "string" ? value : ""))}
        >
          <SelectTrigger size="sm" className="w-40">
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
          <SelectTrigger size="sm" className="w-44">
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

        <Input
          value={actor}
          onChange={(event) => setActor(event.target.value)}
          placeholder="操作者（当前页内筛选）"
          className="h-8 w-48 text-xs"
          aria-label="操作者（当前页内筛选）"
        />
        <Input
          type="datetime-local"
          value={timeFrom}
          onChange={(event) => setTimeFrom(event.target.value)}
          className="h-8 w-[10.5rem] text-xs"
          aria-label="起始时间（当前页内筛选）"
          title="起始时间（当前页内筛选）"
        />
        <span className="text-xs text-faint">至</span>
        <Input
          type="datetime-local"
          value={timeTo}
          onChange={(event) => setTimeTo(event.target.value)}
          className="h-8 w-[10.5rem] text-xs"
          aria-label="结束时间（当前页内筛选）"
          title="结束时间（当前页内筛选）"
        />

        {action || deviceUid || pageFilter ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() =>
              changeFilter(() => {
                setAction("");
                setDeviceUid("");
                setActor("");
                setTimeFrom("");
                setTimeTo("");
              })
            }
          >
            清除筛选
          </Button>
        ) : null}
      </div>

      {pageFilter ? (
        <Banner tone="info">
          “操作者/时间”只在当前页的 {entries.length} 条记录内筛选（服务端只提供 action 与 device_uid 参数）。
        </Banner>
      ) : null}

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
          description={entries.length === 0 ? "筛选条件越少越容易命中；审计从 0.3.0 首次启动开始记录。" : undefined}
        />
      ) : (
        <ul className="divide-y divide-line rounded-panel border border-line bg-panel">
          {visible.map((entry) => (
            <AuditRow
              key={entry.id}
              entry={entry}
              open={expanded === entry.id}
              onToggle={() => setExpanded(expanded === entry.id ? null : entry.id)}
            />
          ))}
        </ul>
      )}

      <Pager total={query.data?.total ?? 0} limit={PAGE} offset={offset} onChange={setOffset} busy={query.isFetching} />
    </PageShell>
  );
}

function AuditRow({ entry, open, onToggle }: { entry: AuditEntry; open: boolean; onToggle: () => void }) {
  const detail = Object.keys(entry.detail ?? {}).length > 0;
  return (
    <li>
      <button type="button" onClick={onToggle} className="flex w-full items-start gap-2 px-2.5 py-2 text-left" aria-expanded={open}>
        {detail ? (
          <ChevronDown
            className={cn("mt-0.5 size-3.5 shrink-0 text-faint transition-transform", !open && "-rotate-90")}
            aria-hidden
          />
        ) : (
          <span className="mt-0.5 size-3.5 shrink-0" aria-hidden />
        )}
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <span className="text-[0.78rem] font-medium text-ink">{ACTION_LABEL[entry.action] ?? entry.action}</span>
            <span className="font-mono text-[0.7rem] text-faint">{entry.action}</span>
          </span>
          <span className="mt-0.5 block truncate text-[0.7rem] text-faint">
            {formatDateTime(entry.ts_ms)} · {entry.actor || "console"}
            {entry.device_name || entry.device_uid ? ` · ${entry.device_name || entry.device_uid}` : ""}
            {entry.ip ? ` · ${entry.ip}` : ""}
          </span>
        </span>
      </button>
      {open && detail ? (
        <pre className="mx-2.5 mb-2 max-h-56 overflow-auto rounded-md border border-line bg-sunken p-2 font-mono text-[0.7rem] leading-relaxed text-soft">
          {JSON.stringify(entry.detail, null, 2)}
        </pre>
      ) : null}
    </li>
  );
}