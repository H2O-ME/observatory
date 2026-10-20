import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Camera, Power, RotateCcw, Trash2, Unplug } from "lucide-react";

import { Meter } from "@/components/meter";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { api, ApiError } from "@/lib/api";
import { formatBytes, formatDateTime, formatDuration } from "@/lib/format";
import type { Device } from "@/lib/types";
import { cn } from "@/lib/utils";

function num(source: Record<string, unknown>, key: string): number | null {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function text(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === "string" ? value : "";
}

interface Disk {
  mount?: string;
  total?: number;
  free?: number;
  percent?: number;
}

interface SessionRow {
  session_id?: number;
  username?: string;
  state?: string;
  is_console?: boolean;
}

interface Props {
  device: Device;
  onRefresh: () => void;
}

export function OverviewView({ device, onRefresh }: Props) {
  const [name, setName] = useState(device.name);
  const [note, setNote] = useState(device.note);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const act = useMutation({
    mutationFn: async (action: { type: string; confirm?: boolean }) =>
      api.createCommand(device.uid, action.type, {}, action.confirm ?? false),
    onSuccess: (command) => {
      setMessage(`已下发命令 ${command.type}（${command.status}）`);
      setError("");
      onRefresh();
    },
    onError: (failure) => {
      setError(failure instanceof ApiError ? failure.detail : "命令下发失败");
      setMessage("");
    },
  });

  const save = useMutation({
    mutationFn: () => api.patchDevice(device.uid, { name, note }),
    onSuccess: () => {
      setMessage("已保存");
      setError("");
      onRefresh();
    },
    onError: (failure) => setError(failure instanceof ApiError ? failure.detail : "保存失败"),
  });

  const uninstall = useMutation({
    mutationFn: () => api.uninstall(device.uid),
    onSuccess: (result) => {
      setMessage(result.message);
      setError("");
      onRefresh();
    },
    onError: (failure) => setError(failure instanceof ApiError ? failure.detail : "卸载失败"),
  });

  const remove = useMutation({
    mutationFn: () => api.deleteDevice(device.uid),
    onSuccess: () => setMessage("设备记录已删除"),
    onError: (failure) => setError(failure instanceof ApiError ? failure.detail : "删除失败"),
  });

  const purgeFrames = useMutation({
    mutationFn: () => api.purgeFrames(device.uid),
    onSuccess: (result) => {
      setMessage(result.message);
      onRefresh();
    },
    onError: (failure) => setError(failure instanceof ApiError ? failure.detail : "清理失败"),
  });

  const cpu = num(device.sysinfo, "cpu_percent");
  const memory = num(device.sysinfo, "mem_percent");
  const disks = Array.isArray(device.sysinfo.disks) ? (device.sysinfo.disks as Disk[]) : [];
  const sessions = Array.isArray(device.sysinfo.sessions) ? (device.sysinfo.sessions as SessionRow[]) : [];
  const capabilities = device.presence.capabilities;

  return (
    <div className="space-y-3">
      {message ? (
        <p className="rounded-md border border-ok/30 bg-ok/10 px-2.5 py-1.5 text-xs text-ok">{message}</p>
      ) : null}
      {error ? (
        <p className="rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger">{error}</p>
      ) : null}

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-2 text-xs font-semibold text-soft">资源占用</h2>
        <div className="grid grid-cols-2 gap-3">
          <Meter label="CPU" value={cpu} />
          <Meter label="内存" value={memory} />
        </div>
        <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-[0.7rem] text-soft">
          <span className="truncate">内存 {formatBytes(num(device.sysinfo, "mem_total"))}</span>
          <span className="truncate">运行 {formatDuration(num(device.sysinfo, "uptime_s"))}</span>
          <span className="truncate">{text(device.sysinfo, "cpu_model") || "—"}</span>
          <span className="truncate">{text(device.sysinfo, "username") || "—"}</span>
        </div>
      </section>

      {disks.length > 0 ? (
        <section className="rounded-panel border border-line bg-panel p-3">
          <h2 className="mb-2 text-xs font-semibold text-soft">磁盘</h2>
          <div className="space-y-2.5">
            {disks.map((disk, index) => (
              <div key={`${disk.mount ?? index}`}>
                <Meter
                  label={`${disk.mount ?? "?"} · 剩余 ${formatBytes(disk.free)} / ${formatBytes(disk.total)}`}
                  value={disk.percent}
                />
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {sessions.length > 0 ? (
        <section className="rounded-panel border border-line bg-panel p-3">
          <h2 className="mb-2 text-xs font-semibold text-soft">登录会话</h2>
          <ul className="space-y-1 text-[0.72rem] text-soft">
            {sessions.map((session) => (
              <li key={session.session_id} className="flex items-center justify-between gap-2">
                <span className="truncate">
                  会话 {session.session_id} · {session.username || "（未登录）"}
                </span>
                <span className="shrink-0 text-faint">{session.state}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-2 text-xs font-semibold text-soft">设备信息</h2>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[0.72rem]">
          <dt className="text-faint">设备标识</dt>
          <dd className="truncate font-mono text-soft">{device.uid}</dd>
          <dt className="text-faint">系统</dt>
          <dd className="truncate text-soft">{device.os_name} {device.os_version}</dd>
          <dt className="text-faint">架构</dt>
          <dd className="truncate text-soft">{device.arch}</dd>
          <dt className="text-faint">受控端</dt>
          <dd className="truncate font-mono text-soft">v{device.agent_version || "?"}</dd>
          <dt className="text-faint">注册时间</dt>
          <dd className="truncate text-soft">{formatDateTime(device.enrolled_at_ms)}</dd>
          <dt className="text-faint">最近上报</dt>
          <dd className="truncate text-soft">{formatDateTime(device.last_seen_ms)}</dd>
          <dt className="text-faint">通道</dt>
          <dd className="truncate text-soft">
            {device.presence.transport === "ws" ? "WebSocket（实时）" : device.presence.transport === "poll" ? "HTTPS 轮询" : "无"}
          </dd>
        </dl>
      </section>

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-2 text-xs font-semibold text-soft">重命名</h2>
        <div className="space-y-2">
          <div className="space-y-1">
            <Label htmlFor="device-name" className="text-[0.7rem] text-faint">
              显示名称
            </Label>
            <Input id="device-name" value={name} onChange={(event) => setName(event.target.value)} className="h-9" />
          </div>
          <div className="space-y-1">
            <Label htmlFor="device-note" className="text-[0.7rem] text-faint">
              备注
            </Label>
            <Input id="device-note" value={note} onChange={(event) => setNote(event.target.value)} className="h-9" />
          </div>
          <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending}>
            保存
          </Button>
        </div>
      </section>

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-2 text-xs font-semibold text-soft">远程操作</h2>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={!capabilities.screenshot || act.isPending}
            onClick={() => act.mutate({ type: "screenshot" })}
          >
            <Camera className="size-3.5" />
            立即截图
          </Button>
          <Button size="sm" variant="outline" disabled={!capabilities.update || act.isPending} onClick={() => act.mutate({ type: "reload_config" })}>
            <RotateCcw className="size-3.5" />
            重新加载配置
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!capabilities.update || act.isPending}
            onClick={() => {
              if (window.confirm("确定重启受控端服务？机器不会重启。")) act.mutate({ type: "restart_agent", confirm: true });
            }}
          >
            <Power className="size-3.5" />
            重启受控端
          </Button>
          <Button
            size="sm"
            variant="destructive"
            disabled={!capabilities.uninstall || uninstall.isPending}
            onClick={() => {
              if (window.confirm("远程卸载会在目标机以 SYSTEM 权限自我清理，不会弹出 UAC。确定继续？"))
                uninstall.mutate();
            }}
          >
            <Unplug className="size-3.5" />
            远程卸载
          </Button>
        </div>
        <p className={cn("mt-2 text-[0.68rem] leading-relaxed", capabilities.uninstall ? "text-faint" : "text-warn")}>
          {capabilities.uninstall
            ? "远程卸载不弹 UAC：受控端服务本身以 SYSTEM 运行，会自行停止、删除并使记录标记为已卸载。"
            : "设备当前不可下发卸载指令（" + device.presence.reason + "）"}
        </p>
      </section>

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-2 text-xs font-semibold text-soft">数据清理</h2>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={purgeFrames.isPending}
            onClick={() => {
              if (window.confirm("删除该设备的所有历史画面？此操作不可恢复。")) purgeFrames.mutate();
            }}
          >
            <Trash2 className="size-3.5" />
            清空历史画面
          </Button>
          <Button
            size="sm"
            variant="destructive"
            disabled={remove.isPending || device.presence.state === "online"}
            onClick={() => {
              if (window.confirm("删除设备记录？设备必须已离线或已卸载。")) remove.mutate();
            }}
          >
            <Trash2 className="size-3.5" />
            删除设备记录
          </Button>
        </div>
        <Separator className="my-2" />
        <p className="text-[0.68rem] leading-relaxed text-faint">
          删除记录会同时清除本地画面与该设备在归档仓库中的文件；在线设备必须先远程卸载，避免留下仍会尝试上报的机器。
        </p>
      </section>
    </div>
  );
}