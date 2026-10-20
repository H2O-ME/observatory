import { useQuery } from "@tanstack/react-query";
import { ChevronDown } from "lucide-react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { Command, Device } from "@/lib/types";
import { useLive } from "@/store/live";
import { cn } from "@/lib/utils";

const STATUS_LABEL: Record<Command["status"], string> = {
  pending: "待下发",
  dispatched: "已下发",
  ok: "成功",
  error: "失败",
  timeout: "超时",
  cancelled: "取消",
};

const STATUS_CLASS: Record<Command["status"], string> = {
  pending: "text-soft",
  dispatched: "text-brand",
  ok: "text-ok",
  error: "text-danger",
  timeout: "text-warn",
  cancelled: "text-faint",
};

const TYPE_LABEL: Record<string, string> = {
  screenshot: "截图",
  collect_now: "立即采集",
  files: "文件操作",
  sysinfo: "系统信息",
  log_tail: "日志",
  update: "更新",
  reload_config: "重载配置",
  restart_agent: "重启受控端",
  uninstall: "卸载",
  reboot: "重启",
  poweroff: "关机",
};

function summarize(command: Command): string {
  if (command.error) return command.error;
  if (!command.result) return "";
  const result = command.result as Record<string, unknown>;
  if (typeof result.error === "string" && result.error) return result.error;
  if (command.type === "screenshot" && typeof result.frame_ts === "number") {
    return `画面 ${formatDateTime(result.frame_ts)}`;
  }
  if (command.type === "files" && typeof result.op === "string") return `op=${result.op}`;
  const keys = Object.keys(result);
  if (keys.length === 0) return "";
  return keys.slice(0, 4).join(", ");
}

export function CommandsView({ device }: { device: Device }) {
  const connected = useLive((state) => state.connected);
  const query = useQuery({
    queryKey: ["commands", device.uid],
    queryFn: () => api.commands(device.uid, 60),
    refetchInterval: connected ? 8_000 : 4_000,
  });

  const commands = query.data?.commands ?? [];

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <h2 className="text-xs font-semibold text-soft">命令记录</h2>
        <Button variant="ghost" size="sm" onClick={() => void query.refetch()}>
          刷新
        </Button>
      </div>

      {commands.length === 0 ? (
        <p className="rounded-panel border border-dashed border-line px-3 py-8 text-center text-xs text-soft">
          还没有下发过命令
        </p>
      ) : (
        <ul className="divide-y divide-line overflow-hidden rounded-panel border border-line bg-panel">
          {commands.map((command) => (
            <li key={command.id}>
              <details className="group">
                <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 px-3 py-2">
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <span className="text-[0.78rem] font-medium text-ink">
                        {TYPE_LABEL[command.type] ?? command.type}
                      </span>
                      <span className={cn("text-[0.68rem]", STATUS_CLASS[command.status])}>
                        {STATUS_LABEL[command.status]}
                      </span>
                    </span>
                    <span className="mt-0.5 block truncate text-[0.66rem] text-faint">
                      {formatDateTime(command.created_at_ms)} · {summarize(command) || "—"}
                    </span>
                  </span>
                  <ChevronDown className="size-3.5 shrink-0 text-faint transition-transform group-open:rotate-180" />
                </summary>
                <div className="space-y-1 border-t border-line/70 bg-sunken/40 px-3 py-2">
                  <p className="font-mono text-[0.62rem] break-all text-faint">id: {command.id}</p>
                  {command.payload && Object.keys(command.payload).length > 0 ? (
                    <pre className="max-h-40 overflow-auto rounded bg-sunken p-2 text-[0.62rem] text-soft">
                      {JSON.stringify(command.payload, null, 2)}
                    </pre>
                  ) : null}
                  {command.result ? (
                    <pre className="max-h-52 overflow-auto rounded bg-sunken p-2 text-[0.62rem] text-soft">
                      {JSON.stringify(command.result, null, 2)}
                    </pre>
                  ) : null}
                  {command.error ? <p className="text-[0.66rem] text-danger">{command.error}</p> : null}
                </div>
              </details>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}