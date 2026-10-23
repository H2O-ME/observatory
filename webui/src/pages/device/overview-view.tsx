import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Camera, Power, RotateCcw, Trash2, TriangleAlert, Unplug } from "lucide-react";

import { Meter } from "@/components/meter";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { api, isApiError } from "@/lib/api";
import type { Device } from "@/lib/types";
import { formatBytes, formatDateTime, formatDuration, formatAge } from "@/lib/format";
import { capabilitiesOf, selfUpdateBlockedReason, transportOf } from "@/lib/presence";
import { cn } from "@/lib/utils";

function num(source: Record<string, unknown>, key: string): number | null {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function text(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === "string" ? value : "";
}

function errorText(failure: unknown, fallback: string): string {
  return isApiError(failure) ? failure.message : fallback;
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

/**
 * Rename form.
 *
 * Local state is seeded from the server value and only re-synced while the form
 * is clean, so a background refetch cannot overwrite what the operator is
 * typing, and switching to another device never inherits the previous name.
 */
function RenameCard({ device, onSaved }: { device: Device; onSaved: () => void }) {
  const [name, setName] = useState(device.name);
  const [note, setNote] = useState(device.note);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    if (dirty) return;
    setName(device.name);
    setNote(device.note);
  }, [device.uid, device.name, device.note, dirty]);

  const save = useMutation({
    mutationFn: () => api.patchDevice(device.uid, { name, note }),
    onSuccess: () => {
      setDirty(false);
      onSaved();
    },
  });

  return (
    <section className="rounded-panel border border-line bg-panel p-3">
      <h2 className="mb-2 text-xs font-semibold text-soft">重命名</h2>
      <div className="space-y-2">
        <div className="space-y-1">
          <Label htmlFor="device-name" className="text-[0.7rem] text-faint">
            显示名称
          </Label>
          <Input
            id="device-name"
            value={name}
            onChange={(event) => {
              setDirty(true);
              setName(event.target.value);
            }}
            className="h-9"
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="device-note" className="text-[0.7rem] text-faint">
            备注
          </Label>
          <Input
            id="device-note"
            value={note}
            onChange={(event) => {
              setDirty(true);
              setNote(event.target.value);
            }}
            className="h-9"
          />
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending}>
            保存
          </Button>
          {save.isError ? <span className="text-[0.7rem] text-danger">{errorText(save.error, "保存失败")}</span> : null}
        </div>
      </div>
    </section>
  );
}

/**
 * Overview.
 *
 * Capability gating is literal (A13): every control reads its flag from the
 * server's `capabilities` block. Nothing here derives "can I do this?" from the
 * device state or from a timestamp.
 */
export function OverviewView({ device, onRefresh }: Props) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  /** One dialog for all destructive actions; `null` = closed. */
  const [pending, setPending] = useState<null | "restart" | "uninstall" | "purge" | "delete">(null);

  const capabilities = capabilitiesOf(device.presence);
  const transport = transportOf(device.presence);
  const capture = device.presence.capture;
  const captureBlocked =
    !capabilities.screenshot || capture?.state === "no_session" || capture?.state === "blocked";
  const selfUpdateBlocked = selfUpdateBlockedReason(device.presence);
  // The command bus is reachable exactly while the device has a live carrier;
  // `files` is the capability that encodes that (A13: no live channel → files=false).
  const commandBusAvailable = capabilities.files;

  const act = useMutation({
    mutationFn: (action: { type: string; confirm?: boolean }) =>
      api.createCommand(device.uid, action.type, {}, action.confirm ?? false),
    onSuccess: (command) => {
      setMessage(`已下发命令 ${command.type}（${command.status}）`);
      setError("");
      setPending(null);
      onRefresh();
    },
    onError: (failure) => {
      setError(errorText(failure, "命令下发失败"));
      setMessage("");
      setPending(null);
    },
  });

  const uninstall = useMutation({
    mutationFn: () => api.uninstallDevice(device.uid),
    onSuccess: (result) => {
      setMessage(result.message);
      setError("");
      setPending(null);
      onRefresh();
    },
    onError: (failure) => {
      setError(errorText(failure, "卸载失败"));
      setPending(null);
    },
  });

  const purgeFrames = useMutation({
    mutationFn: () => api.purgeFrames(device.uid),
    onSuccess: (result) => {
      setMessage(result.message);
      setError("");
      setPending(null);
      void queryClient.invalidateQueries({ queryKey: ["device", device.uid] });
      void queryClient.invalidateQueries({ queryKey: ["devices"] });
      void queryClient.invalidateQueries({ queryKey: ["timeline"] });
      void queryClient.invalidateQueries({ queryKey: ["frames"] });
      onRefresh();
    },
    onError: (failure) => {
      setError(errorText(failure, "清理失败"));
      setPending(null);
    },
  });

  const remove = useMutation({
    mutationFn: () => api.deleteDevice(device.uid),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["devices"] });
      navigate("/wall", { replace: true });
    },
    onError: (failure) => {
      setError(errorText(failure, "删除失败"));
      setPending(null);
    },
  });

  const cpu = num(device.sysinfo, "cpu_percent");
  const memory = num(device.sysinfo, "mem_percent");
  const disks = Array.isArray(device.sysinfo.disks) ? (device.sysinfo.disks as Disk[]) : [];
  const sessions = Array.isArray(device.sysinfo.sessions) ? (device.sysinfo.sessions as SessionRow[]) : [];
  const canDeleteRecord = device.presence.state === "offline" || device.presence.state === "uninstalled";

  return (
    <div className="space-y-3">
      {message ? (
        <p role="status" aria-live="polite" className="rounded-md border border-ok/30 bg-ok/10 px-2.5 py-1.5 text-xs text-ok">
          {message}
        </p>
      ) : null}
      {error ? (
        <p role="alert" aria-live="polite" className="rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger">
          {error}
        </p>
      ) : null}

      {captureBlocked ? (
        <section className="rounded-panel border border-warn/40 bg-warn/10 p-3">
          <h2 className="mb-1 flex items-center gap-1.5 text-xs font-semibold text-warn">
            <Camera className="size-3.5" aria-hidden />
            无法采集屏幕
          </h2>
          <p className="text-[0.72rem] leading-relaxed text-soft">
            {capture?.reason || device.presence.reason || "受控端当前没有可采集的桌面"}
          </p>
          <p className="mt-1 text-[0.68rem] leading-relaxed text-faint">
            受控端服务运行在会话 0，只有用户登录并保持桌面会话时才能截屏。请在该机器上登录桌面（远程桌面窗口
            断开也会停止采集），随后画面会自动恢复。
          </p>
        </section>
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
          <dd className="truncate text-soft">
            {device.os_name} {device.os_version}
          </dd>
          <dt className="text-faint">架构</dt>
          <dd className="truncate text-soft">{device.arch}</dd>
          <dt className="text-faint">受控端</dt>
          <dd className="truncate font-mono text-soft">
            v{device.agent_version || "?"}
            {device.protocol ? ` · 协议 ${device.protocol}` : ""}
          </dd>
          <dt className="text-faint">注册时间</dt>
          <dd className="truncate text-soft">{formatDateTime(device.enrolled_at_ms)}</dd>
          <dt className="text-faint">最近上报</dt>
          <dd className="truncate text-soft">
            {formatDateTime(device.last_seen_ms)}（{formatAge(device.presence.age_ms)}）
          </dd>
          <dt className="text-faint">通道</dt>
          <dd className="truncate text-soft">
            {transport === "ws" ? "WebSocket（实时）" : transport === "poll" ? "HTTP 轮询（无实时控制）" : "无活动通道"}
          </dd>
        </dl>
      </section>

      {selfUpdateBlocked ? (
        <section className="rounded-panel border border-warn/40 bg-warn/10 p-3">
          <h2 className="mb-1 flex items-center gap-1.5 text-xs font-semibold text-warn">
            <TriangleAlert className="size-3.5" aria-hidden />
            无法自动升级
          </h2>
          <p className="text-[0.72rem] leading-relaxed text-soft">{selfUpdateBlocked}</p>
          <p className="mt-1 text-[0.68rem] leading-relaxed text-faint">
            该版本不在自动更新路径上，服务器不会向它推送新版本。请在目标机器上重新运行最新的安装脚本（覆盖安装，设备标识与历史记录都会保留）。
          </p>
        </section>
      ) : null}

      <RenameCard device={device} onSaved={onRefresh} />

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-2 text-xs font-semibold text-soft">远程操作</h2>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={!capabilities.screenshot || act.isPending}
            title={capabilities.screenshot ? "让受控端立即截一张图" : "受控端当前无法截图"}
            onClick={() => act.mutate({ type: "screenshot" })}
          >
            <Camera className="size-3.5" />
            立即截图
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!commandBusAvailable || act.isPending}
            title={commandBusAvailable ? "重新加载受控端配置" : "没有可用的命令通道"}
            onClick={() => act.mutate({ type: "reload_config" })}
          >
            <RotateCcw className="size-3.5" />
            重新加载配置
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!commandBusAvailable || act.isPending}
            title={commandBusAvailable ? "重启受控端服务（不会重启机器）" : "没有可用的命令通道"}
            onClick={() => setPending("restart")}
          >
            <Power className="size-3.5" />
            重启受控端
          </Button>
          <Button
            size="sm"
            variant="destructive"
            disabled={!capabilities.uninstall || uninstall.isPending}
            title={capabilities.uninstall ? "以 SYSTEM 权限自我清理，不弹 UAC" : "设备当前不可下发卸载指令"}
            onClick={() => setPending("uninstall")}
          >
            <Unplug className="size-3.5" />
            远程卸载
          </Button>
        </div>
        <p className={cn("mt-2 text-[0.68rem] leading-relaxed", capabilities.uninstall ? "text-faint" : "text-warn")}>
          {capabilities.uninstall
            ? "远程卸载不弹 UAC：受控端服务本身以 SYSTEM 运行，会自行停止、删除并使记录标记为已卸载。"
            : `设备当前不可下发卸载指令（${device.presence.reason}）`}
        </p>
      </section>

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-2 text-xs font-semibold text-soft">数据清理</h2>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={purgeFrames.isPending}
            onClick={() => setPending("purge")}
          >
            <Trash2 className="size-3.5" />
            清空历史画面
          </Button>
          <Button
            size="sm"
            variant="destructive"
            disabled={remove.isPending || !canDeleteRecord}
            title={canDeleteRecord ? "删除这台设备的历史记录" : "设备仍有活动通道，请先远程卸载"}
            onClick={() => setPending("delete")}
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

      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(open) => {
          if (!open) setPending(null);
        }}
        title={
          pending === "restart"
            ? "重启受控端服务"
            : pending === "uninstall"
              ? "远程卸载"
              : pending === "purge"
                ? "清空历史画面"
                : "删除设备记录"
        }
        description={
          pending === "restart"
            ? "受控端服务会立即重启，机器不会重启；重启期间实时控制与文件操作会短暂中断。"
            : pending === "uninstall"
              ? "受控端服务以 SYSTEM 权限自我清理：停止服务、删除程序与自启动，不会弹出 UAC。设备记录随后标记为已卸载。"
              : pending === "purge"
                ? "删除该设备在本地的全部历史画面与缩略图，不可恢复；归档仓库中的远端副本不受影响。"
                : "删除设备记录并清理本地数据；设备必须已离线或已卸载。此操作不可撤销。"
        }
        confirmLabel={pending === "delete" ? "删除记录" : pending === "uninstall" ? "卸载" : "继续"}
        busy={act.isPending || uninstall.isPending || purgeFrames.isPending || remove.isPending}
        onConfirm={() => {
          if (pending === "restart") act.mutate({ type: "restart_agent", confirm: true });
          else if (pending === "uninstall") uninstall.mutate();
          else if (pending === "purge") purgeFrames.mutate();
          else if (pending === "delete") remove.mutate();
        }}
      />
    </div>
  );
}