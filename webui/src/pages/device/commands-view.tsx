import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, RefreshCw } from "lucide-react";

import { Timeline, TimelineItem } from "@/components/aceternity";
import { Banner, EmptyBlock, ErrorBlock, LoadingBlock } from "@/components/async-state";
import { Button } from "@/components/ui/button";
import { useViewport } from "@/hooks/use-viewport";
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

/** Token surfaces only — Tailwind alpha utilities are dropped on Chromium 81. */
const STATUS_TONE: Record<CommandStatus, string> = {
  pending: "border-line bg-sunken text-soft",
  dispatched: "border-line bg-brand-soft text-brand",
  ok: "border-line bg-ok-soft text-ok",
  error: "border-line bg-danger-soft text-danger",
  timeout: "border-line bg-warn-soft text-warn",
  cancelled: "border-line bg-sunken text-faint",
};

/** Timeline marker colour per status (dot tone). */
const STATUS_DOT: Record<CommandStatus, string> = {
  pending: "bg-faint animate-sm-pulse",
  dispatched: "bg-brand",
  ok: "bg-ok",
  error: "bg-danger",
  timeout: "bg-warn",
  cancelled: "bg-faint",
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
 *
 * The device shell already shows identity/liveness, so this view deliberately
 * has no page header: one compact toolbar, then the journal.
 */
export function CommandsView({ device }: { device: Device }) {
  const viewport = useViewport();
  const watch = viewport === "watch";
  const [offset, setOffset] = useState(0);
  const [expanded, setExpanded] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["commands", device.uid, PAGE, offset],
    queryFn: () => api.listCommands(device.uid, PAGE, offset),
    // Fast poll only while something is in flight (the socket invalidates on
    // command_created/updated anyway); a settled journal drops to 30s repair
    // polling instead of a permanent 5s loop.
    refetchInterval: (query) => {
      const commands = query.state.data?.commands;
      const active = commands?.some(
        (command) => command.status === "pending" || command.status === "dispatched",
      );
      return active ? 5_000 : 30_000;
    },
  });

  const commands = query.data?.commands ?? [];
  const busy = query.isFetching;
  const page = Math.floor(offset / PAGE) + 1;

  return (
    <div
      className={cn(
        "w-full max-w-full min-w-0 overflow-x-hidden",
        watch ? "space-y-3" : "space-y-5",
      )}
    >
      <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-3">
        <p className="min-w-0 truncate text-[0.74rem] text-soft">
          第 {page} 页 · 本页 {commands.length} 条 · 每 5 秒刷新
        </p>
        <Button
          size="sm"
          variant="outline"
          className="tap"
          onClick={() => void query.refetch()}
          disabled={busy}
        >
          <RefreshCw className={cn("mr-2 size-4", busy && "animate-spin")} aria-hidden />
          刷新
        </Button>
      </div>

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
              <Button
                size="sm"
                variant="outline"
                className="tap"
                onClick={() => setOffset(Math.max(0, offset - PAGE))}
              >
                返回上一页
              </Button>
            ) : undefined
          }
        />
      ) : (
        <div className="surface min-w-0 p-4 sm:p-5">
          <Timeline>
            {commands.map((command) => {
              const open = expanded === command.id;
              const status = (command.status ?? "pending") as CommandStatus;
              const errorText = command.error
                ? `${command.error_code ? `${ERROR_CODE_TEXT[command.error_code] ?? command.error_code}：` : ""}${command.error}`
                : null;
              return (
                <TimelineItem
                  key={command.id}
                  className="pt-2"
                  date={formatDateTime(command.created_at_ms)}
                  icon={<span className={cn("size-2 rounded-full", STATUS_DOT[status])} aria-hidden />}
                  title={
                    <button
                      type="button"
                      onClick={() => setExpanded(open ? null : command.id)}
                      aria-expanded={open}
                      className="tap-row grid w-full min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start rounded-lg px-0.5 text-left"
                    >
                      <span className="min-w-0">
                        <span className="flex min-w-0 flex-wrap items-center">
                          <span
                            className={cn(
                              "mr-2 min-w-0 font-semibold text-ink",
                              watch ? "text-[0.82rem]" : "text-[0.88rem]",
                            )}
                          >
                            {TYPE_LABEL[command.type] ?? command.type}
                          </span>
                          <span
                            className={cn(
                              "mr-2 shrink-0 rounded-md border px-2 py-0.5 text-[0.7rem] font-medium",
                              STATUS_TONE[status],
                            )}
                          >
                            {STATUS_LABEL[status] ?? status}
                          </span>
                          {command.attempts > 1 ? (
                            <span className="mr-2 shrink-0 text-[0.7rem] text-warn">
                              重投 {command.attempts} 次
                            </span>
                          ) : null}
                        </span>
                        <span className="mt-1.5 block min-w-0 truncate text-[0.72rem] text-faint">
                          预算 {command.deadline_ms / 1000}s · 耗时 {elapsedOf(command)} ·{" "}
                          <span className="font-mono">{command.id.slice(0, 8)}</span>
                        </span>
                        {errorText ? (
                          <span
                            className={cn(
                              "mt-1 block text-[0.72rem] leading-relaxed text-danger",
                              !open && "truncate",
                            )}
                          >
                            {errorText}
                          </span>
                        ) : null}
                      </span>
                      <ChevronDown
                        className="ml-2 mt-0.5 size-4 shrink-0 text-faint transition-transform"
                        style={{ transform: open ? "rotate(0deg)" : "rotate(-90deg)" }}
                        aria-hidden
                      />
                    </button>
                  }
                >
                  {open ? (
                    <div className="animate-sm-in space-y-2.5 rounded-xl border border-line bg-sunken p-2.5 sm:p-3.5">
                      <dl
                        className={cn(
                          "grid min-w-0 gap-x-3 gap-y-1.5 text-[0.74rem]",
                          watch ? "grid-cols-1" : "grid-cols-[5.5rem_minmax(0,1fr)]",
                        )}
                      >
                        <dt className="text-faint">命令 id</dt>
                        <dd className="break-all font-mono text-soft">{command.id}</dd>
                        <dt className="text-faint">下发时间</dt>
                        <dd className="text-soft">{formatDateTime(command.dispatched_at_ms ?? 0)}</dd>
                        <dt className="text-faint">结束时间</dt>
                        <dd className="text-soft">{formatDateTime(command.finished_at_ms ?? 0)}</dd>
                        <dt className="text-faint">错误码</dt>
                        <dd className="break-words text-soft">
                          {command.error_code
                            ? `${command.error_code} · ${ERROR_CODE_TEXT[command.error_code] ?? "未知"}`
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
                </TimelineItem>
              );
            })}
          </Timeline>
        </div>
      )}

      <div className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          className="tap"
          disabled={busy || offset <= 0}
          onClick={() => setOffset(Math.max(0, offset - PAGE))}
        >
          上一页
        </Button>
        <span className="tnum min-w-0 truncate text-center text-[0.72rem] text-soft">
          第 {page} 页 · {commands.length} / {PAGE} 条
        </span>
        <Button
          size="sm"
          variant="outline"
          className="tap"
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
    <div className="min-w-0">
      <p className="mb-1 text-[0.72rem] font-medium text-faint">{label}</p>
      <pre className="max-h-56 max-w-full overflow-auto rounded-lg border border-line bg-panel p-2.5 font-mono text-[0.7rem] leading-relaxed text-soft">
        {text}
      </pre>
    </div>
  );
}
