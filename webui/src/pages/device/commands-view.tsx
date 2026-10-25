import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, RefreshCw } from "lucide-react";

import { Banner, EmptyBlock, ErrorBlock, LoadingBlock, PageHeader } from "@/components/async-state";
import { Button } from "@/components/ui/button";
import { api, isApiError } from "@/lib/api";
import { formatDateTime, formatDuration } from "@/lib/format";
import type { Command, CommandStatus, Device } from "@/lib/types";
import { cn } from "@/lib/utils";

const PAGE = 25;

const TYPE_LABEL: Record<string, string> = {
  screenshot: "立即截图",
  collect_now: "立即采集",
  sysinfo: "系统信息",
  files: "文件操作",
  log_tail: "读取日志",
  update: "升级受控端",
  reload_config: "重载配置",
  restart_agent: "重启受控端",
  uninstall: "远程卸载",
  reboot: "重启机器",
  poweroff: "关机",
};

const STATUS_LABEL: Record<CommandStatus, string> = {
  pending: "排队中",
  dispatched: "已下发",
  ok: "成功",
  error: "失败",
  timeout: "超时",
  cancelled: "已取消",
};

const STATUS_TONE: Record<CommandStatus, string> = {
  pending: "border-line bg-sunken text-soft",
  dispatched: "border-brand/30 bg-brand/10 text-brand",
  ok: "border-ok/30 bg-ok/10 text-ok",
  error: "border-danger/30 bg-danger/10 text-danger",
  timeout: "border-warn/40 bg-warn/10 text-warn",
  cancelled: "border-line bg-sunken text-faint",
};

/** `error_code` is the machine-readable reason from the unified error contract. */
const ERROR_CODE_TEXT: Record<string, string> = {
  GONE: "设备/链路已不存在（设备离线或会话被关闭）",
  UNSUPPORTED: "受控端不支持该操作，或当前状态不允许",
  TIMEOUT: "受控端在预算内没有响应",
  RATE: "被限流，稍后重试",
  AUTH: "认证失败",
  BAD_REQUEST: "请求被受控端拒绝",
  INTERNAL: "受控端内部错误",
};

function elapsedOf(command: Command): string {
  if (!command.dispatched_at_ms) return "—";
  const end = command.finished_at_ms ?? 0;
  if (!end) return "进行中";
  return formatDuration(Math.max(0, (end - command.dispatched_at_ms) / 1000));
}

/**
 * Command journal for one device.
 *
 * The list is server-paginated (`limit`/`offset`; the server does not return a
 * total, so "next page" is enabled by a full page). While anything is still in
 * flight the page polls, and the console socket additionally invalidates this
 * query on `command_created` / `command_updated`.
 */
export function CommandsView({ device }: { device: Device }) {
  const [offset, setOffset] = useState(0);
  const [expanded, setExpanded] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["commands", device.uid, PAGE, offset],
    queryFn: () => api.listCommands(device.uid, PAGE, offset),
    refetchInterval: 5_000,
  });

  const commands = query.data?.commands ?? [];
  const busy = query.isFetching;

  return (
    <div className="space-y-3">
      <PageHeader
        title="命令记录"
        subtitle={`第 ${Math.floor(offset / PAGE) + 1} 页 · 本页 ${commands.length} 条 · 每 5 秒刷新`}
        actions={
          <Button size="sm" variant="outline" onClick={() => void query.refetch()} disabled={busy}>
            <RefreshCw className={cn("size-3.5", busy && "animate-spin")} />
            刷新
          </Button>
        }
      />

      {query.isError ? (
        <ErrorBlock
          message={isApiError(query.error) ? query.error.message : "无法读取命令记录"}
          retryable
          onRetry={() => void query.refetch()}
        />
      ) : query.isLoading ? (
        <LoadingBlock label="正在读取命令记录…" rows={4} />
      ) : commands.length === 0 ? (
        <EmptyBlock
          title={offset > 0 ? "这一页没有更多记录" : "还没有命令记录"}
          description="截图、文件操作、升级与卸载都会在这里留下一条带 deadline 与错误码的记录。"
          action={
            offset > 0 ? (
              <Button size="sm" variant="outline" onClick={() => setOffset(Math.max(0, offset - PAGE))}>
                返回上一页
              </Button>
            ) : undefined
          }
        />
      ) : (
        <ul className="divide-y divide-line rounded-panel border border-line bg-panel">
          {commands.map((command) => {
            const open = expanded === command.id;
            const status = (command.status ?? "pending") as CommandStatus;
            return (
              <li key={command.id}>
                <button
                  type="button"
                  onClick={() => setExpanded(open ? null : command.id)}
                  className="flex w-full items-start gap-2 px-2.5 py-2 text-left"
                  aria-expanded={open}
                >
                  <ChevronDown
                    className={cn("mt-0.5 size-3.5 shrink-0 text-faint transition-transform", !open && "-rotate-90")}
                    aria-hidden
                  />
                  <span className="min-w-0 flex-1">
                    <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <span className="text-[0.8rem] font-medium text-ink">
                        {TYPE_LABEL[command.type] ?? command.type}
                      </span>
                      <span className={cn("rounded border px-1.5 py-0.5 text-[0.7rem]", STATUS_TONE[status])}>
                        {STATUS_LABEL[status] ?? status}
                      </span>
                      {command.attempts > 1 ? (
                        <span className="text-[0.7rem] text-warn">重投 {command.attempts} 次</span>
                      ) : null}
                    </span>
                    <span className="mt-0.5 block truncate text-[0.7rem] text-faint">
                      {formatDateTime(command.created_at_ms)} · 预算 {command.deadline_ms / 1000}s · 耗时{" "}
                      {elapsedOf(command)} · <span className="font-mono">{command.id.slice(0, 8)}</span>
                    </span>
                    {command.error && !open ? (
                      <span className="mt-0.5 block truncate text-[0.7rem] text-danger">
                        {command.error_code ? `${ERROR_CODE_TEXT[command.error_code] ?? command.error_code}：` : ""}
                        {command.error}
                      </span>
                    ) : null}
                  </span>
                </button>

                {open ? (
                  <div className="space-y-2 border-t border-line px-2.5 py-2">
                    <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                      <dt className="text-faint">命令 id</dt>
                      <dd className="break-all font-mono text-soft">{command.id}</dd>
                      <dt className="text-faint">下发时间</dt>
                      <dd className="text-soft">{formatDateTime(command.dispatched_at_ms ?? 0)}</dd>
                      <dt className="text-faint">结束时间</dt>
                      <dd className="text-soft">{formatDateTime(command.finished_at_ms ?? 0)}</dd>
                      <dt className="text-faint">错误码</dt>
                      <dd className="text-soft">
                        {command.error_code
                          ? `${command.error_code} · ${ERROR_CODE_TEXT[command.error_code] ?? "未知" }`
                          : "—"}
                      </dd>
                    </dl>
                    {command.error ? <Banner tone="error">{command.error}</Banner> : null}
                    <JsonBlock label="请求参数" value={command.payload} />
                    {command.result !== null && command.result !== undefined ? (
                      <JsonBlock label="执行结果" value={command.result} />
                    ) : null}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}

      <div className="flex items-center justify-between text-xs text-soft">
        <Button
          size="sm"
          variant="outline"
          disabled={busy || offset <= 0}
          onClick={() => setOffset(Math.max(0, offset - PAGE))}
        >
          上一页
        </Button>
        <span>本页 {commands.length} / {PAGE} 条</span>
        <Button
          size="sm"
          variant="outline"
          disabled={busy || commands.length < PAGE}
          onClick={() => setOffset(offset + PAGE)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  let text: string;
  try {
    text = JSON.stringify(value, null, 2);
  } catch {
    text = String(value);
  }
  if (text.length > 4000) text = `${text.slice(0, 4000)}\n…（已截断）`;
  return (
    <div>
      <p className="mb-1 text-[0.7rem] text-faint">{label}</p>
      <pre className="max-h-56 overflow-auto rounded-md border border-line bg-sunken p-2 font-mono text-[0.7rem] leading-relaxed text-soft">
        {text}
      </pre>
    </div>
  );
}