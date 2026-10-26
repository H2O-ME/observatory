import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  Camera,
  CheckCircle2,
  Power,
  RotateCcw,
  Trash2,
  TriangleAlert,
  Unplug,
} from "lucide-react";

import { Meter, RingMeter } from "@/components/meter";
import { Panel, PanelRow } from "@/components/panel";
import { Banner } from "@/components/async-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { api, isApiError } from "@/lib/api";
import type { Device } from "@/lib/types";
import { formatBytes, formatDateTime, formatDuration, formatAge } from "@/lib/format";
import { capabilitiesOf, selfUpdateBlockedReason, transportOf } from "@/lib/presence";
import { useViewport } from "@/hooks/use-viewport";
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

/** Ring plus the percentage beside it: the chart carries shape, the number carries weight. */
function UsageGauge({
  label,
  value,
  size,
}: {
  label: string;
  value: number | null;
  size: number;
}) {
  return (
    <div className="flex min-w-0 flex-col items-center">
      <RingMeter
        label={label}
        value={value}
        showValue={false}
        showLabel={false}
        size={size}
        stroke={6}
      />
      <span className="tnum mt-2 text-2xl leading-8 font-semibold tracking-[-0.02em] text-foreground">
        {value === null ? "—" : `${Math.round(value)}%`}
      </span>
      <span className="mt-1 text-xs text-muted-foreground">{label}</span>
    </div>
  );
}

/**
 * Rename form.
 *
 * Local state is seeded from the server value and only re-synced while the form
 * is clean, so a background refetch cannot overwrite what the operator is
 * typing, and switching to another device never inherits the previous name.
 */
function RenamePanel({ device, onSaved }: { device: Device; onSaved: () => void }) {
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
    <Panel
      title="重命名"
      hint="显示名称与备注只影响控制台，不会下发到设备。"
      actions={dirty ? <span className="text-xs text-warning">未保存</span> : null}
      className="min-w-0"
      bodyClassName="px-6 pt-1 pb-4"
    >
      <div className="space-y-4">
        <Field>
          <FieldLabel htmlFor="device-name">显示名称</FieldLabel>
          <Input
            id="device-name"
            className="w-full border-transparent bg-muted focus-visible:ring-1 focus-visible:ring-ring"
            value={name}
            onChange={(event) => {
              setDirty(true);
              setName(event.target.value);
            }}
          />
        </Field>
        <Field>
          <FieldLabel htmlFor="device-note">备注</FieldLabel>
          <Input
            id="device-note"
            className="w-full border-transparent bg-muted focus-visible:ring-1 focus-visible:ring-ring"
            value={note}
            onChange={(event) => {
              setDirty(true);
              setNote(event.target.value);
            }}
          />
        </Field>
        <Button
          className="h-9 w-24 rounded-md"
          onClick={() => save.mutate()}
          disabled={save.isPending}
        >
          保存
        </Button>
        {save.isError ? (
          <span className="text-xs text-destructive">{errorText(save.error, "保存失败")}</span>
        ) : null}
      </div>
    </Panel>
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
  const viewport = useViewport();
  const watch = viewport === "watch";
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  /** One dialog for all destructive actions; `null` = closed. */
  const [pending, setPending] = useState<null | "restart" | "uninstall" | "purge" | "delete">(null);

  const capabilities = capabilitiesOf(device.presence);
  const transport = transportOf(device.presence);
  const capture = device.presence.capture;
  const captureBlocked =
    !capabilities.screenshot || capture?.state === "no_session" || capture?.state === "blocked";
  // The hero already prints the presence reason; only show the capture reason
  // here when it says something new.
  const captureReason = (capture?.reason || "").trim();
  const captureReasonIsNew = captureReason !== "" && captureReason !== device.presence.reason.trim();
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
  const memTotal = num(device.sysinfo, "mem_total");
  const disks = Array.isArray(device.sysinfo.disks) ? (device.sysinfo.disks as Disk[]) : [];
  const sessions = Array.isArray(device.sysinfo.sessions) ? (device.sysinfo.sessions as SessionRow[]) : [];
  const canDeleteRecord = device.presence.state === "offline" || device.presence.state === "uninstalled";

  const bodyText = watch ? "text-xs" : "text-xs";
  const metaText = watch ? "text-xs" : "text-xs";

  return (
    <div className={cn("min-w-0 pb-10", watch ? "space-y-3" : "space-y-4")}>
      {message ? (
        <Banner tone="ok" icon={CheckCircle2}>
          {message}
        </Banner>
      ) : null}
      {error ? (
        <Banner tone="error" icon={TriangleAlert}>
          {error}
        </Banner>
      ) : null}

      {captureBlocked ? (
        <Banner tone="warn" icon={Camera} title="无法采集屏幕">
          {captureReasonIsNew ? <p>{captureReason}</p> : null}
          <p className={cn("text-muted-foreground", metaText, captureReasonIsNew && "mt-1")}>
            受控端服务运行在会话 0，需要保持桌面会话登录；登录后画面会自动恢复。
          </p>
        </Banner>
      ) : null}

      <div className="grid min-w-0 grid-cols-1 items-start gap-6 lg:grid-cols-3">
        <Panel title="资源占用" className="min-w-0 lg:col-span-2">
          <div className="flex min-w-0 flex-col items-center gap-6 sm:flex-row sm:gap-10">
            <div className="flex w-full shrink-0 items-center justify-center gap-6 sm:w-[200px]">
              <UsageGauge label="CPU" value={cpu} size={watch ? 68 : 80} />
              <UsageGauge label="内存" value={memory} size={watch ? 68 : 80} />
            </div>
            <dl className="grid w-full min-w-0 flex-1 grid-cols-2 gap-x-4 gap-y-6 sm:w-auto">
              <div className="min-w-0">
                <dt className="text-sm text-muted-foreground">内存总量</dt>
                <dd
                  className="tnum mt-1 truncate text-sm font-medium text-foreground"
                  title={formatBytes(memTotal)}
                >
                  {formatBytes(memTotal)}
                </dd>
              </div>
              <div className="min-w-0">
                <dt className="text-sm text-muted-foreground">运行时长</dt>
                <dd
                  className="mt-1 truncate text-sm font-medium text-foreground"
                  title={formatDuration(num(device.sysinfo, "uptime_s"))}
                >
                  {formatDuration(num(device.sysinfo, "uptime_s"))}
                </dd>
              </div>
              <div className="min-w-0">
                <dt className="text-sm text-muted-foreground">处理器</dt>
                <dd
                  className="mt-1 truncate text-sm font-medium text-foreground"
                  title={text(device.sysinfo, "cpu_model") || "—"}
                >
                  {text(device.sysinfo, "cpu_model") || "—"}
                </dd>
              </div>
              <div className="min-w-0">
                <dt className="text-sm text-muted-foreground">当前用户</dt>
                <dd
                  className="mt-1 truncate text-sm font-medium text-foreground"
                  title={text(device.sysinfo, "username") || "—"}
                >
                  {text(device.sysinfo, "username") || "—"}
                </dd>
              </div>
            </dl>
          </div>
        </Panel>

        <div className="flex min-w-0 flex-col gap-6 lg:col-span-1">
          {disks.length > 0 ? (
            <Panel title="磁盘" className="min-w-0">
              <div className="space-y-3">
                {disks.map((disk, index) => {
                  // Bar length = 已用占比, derived from the server's total/free
                  // pair (fall back to the agent's own percent when totals are
                  // missing). High fill therefore reads as "almost full".
                  const total = disk.total ?? 0;
                  const used = total > 0 ? Math.max(0, total - (disk.free ?? 0)) : 0;
                  const usedPercent =
                    total > 0
                      ? (used / total) * 100
                      : typeof disk.percent === "number"
                        ? disk.percent
                        : null;
                  return (
                    <div key={`${disk.mount ?? index}`} className="min-w-0">
                      <Meter
                        label={`${disk.mount ?? "?"} · 已用 ${formatBytes(used)}`}
                        value={usedPercent}
                        hint={`共 ${formatBytes(total)} · 剩余 ${formatBytes(disk.free ?? 0)}`}
                      />
                    </div>
                  );
                })}
              </div>
            </Panel>
          ) : null}

          {sessions.length > 0 ? (
            <Panel title="登录会话" className="min-w-0">
              <ul className="space-y-2">
                {sessions.map((session) => (
                  <li
                    key={session.session_id}
                    className={cn(
                      "grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2 text-foreground",
                      bodyText,
                    )}
                  >
                    <span className="min-w-0 truncate">
                      会话 {session.session_id} · {session.username || "（未登录）"}
                    </span>
                    <span className={cn("shrink-0 text-muted-foreground", metaText)}>{session.state}</span>
                  </li>
                ))}
              </ul>
            </Panel>
          ) : null}
        </div>
      </div>

      {selfUpdateBlocked ? (
        <Banner tone="warn" icon={TriangleAlert} title="无法自动升级">
          <p>{selfUpdateBlocked}</p>
          <p className={cn("mt-1 text-muted-foreground", metaText)}>
            该版本不在自动更新路径上，服务器不会向它推送新版本。请在目标机器上重新运行最新的安装脚本（覆盖安装，设备标识与历史记录都会保留）。
          </p>
        </Banner>
      ) : null}

      <div className="grid min-w-0 grid-cols-1 items-start gap-6 lg:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-6">
          <Panel title="设备信息" className="min-w-0">
            <dl className="grid min-w-0 grid-cols-1 gap-x-12 gap-y-4 sm:grid-cols-2">
              <div className="min-w-0 sm:col-span-2">
                <PanelRow label="设备标识">
                  <span className="tnum truncate font-mono text-xs" title={device.uid}>
                    {device.uid}
                  </span>
                </PanelRow>
              </div>
              <div className="min-w-0">
                <PanelRow label="系统">
                  <span className="truncate" title={`${device.os_name} ${device.os_version}`}>
                    {device.os_name} {device.os_version}
                  </span>
                </PanelRow>
              </div>
              <div className="min-w-0">
                <PanelRow label="架构">
                  <span className="truncate">{device.arch}</span>
                </PanelRow>
              </div>
              <div className="min-w-0">
                <PanelRow label="受控端">
                  <span className="tnum truncate">
                    v{device.agent_version || "?"}
                    {device.protocol ? ` · 协议 ${device.protocol}` : ""}
                  </span>
                </PanelRow>
              </div>
              <div className="min-w-0">
                <PanelRow label="通道">
                  <span className="truncate">
                    {transport === "ws"
                      ? "WebSocket（实时）"
                      : transport === "poll"
                        ? "HTTP 轮询（无实时控制）"
                        : "无活动通道"}
                  </span>
                </PanelRow>
              </div>
              <div className="min-w-0">
                <PanelRow label="注册时间">
                  <span className="truncate">{formatDateTime(device.enrolled_at_ms)}</span>
                </PanelRow>
              </div>
              <div className="min-w-0">
                <PanelRow label="最近上报">
                  <span className="truncate">
                    {formatDateTime(device.last_seen_ms)}（{formatAge(device.presence.age_ms)}）
                  </span>
                </PanelRow>
              </div>
            </dl>
          </Panel>

          <Panel
            title="远程操作"
            hint="命令通道可用时才能下发；重启只影响受控端服务。"
            flush
            className="min-w-0"
          >
            <ul className="flex flex-col">
              <li className="flex items-center justify-between gap-3 border-b border-border-soft px-6 py-4 last:border-0">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-foreground">立即截图</p>
                  <p className="mt-1 text-xs text-muted-foreground">让受控端立即截一张图</p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  className="h-8"
                  disabled={!capabilities.screenshot || act.isPending}
                  title={capabilities.screenshot ? "让受控端立即截一张图" : "受控端当前无法截图"}
                  onClick={() => act.mutate({ type: "screenshot" })}
                >
                  <Camera className="size-3.5" aria-hidden />
                  截图
                </Button>
              </li>
              <li className="flex items-center justify-between gap-3 border-b border-border-soft px-6 py-4 last:border-0">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-foreground">重新加载配置</p>
                  <p className="mt-1 text-xs text-muted-foreground">不重启服务，重新读取服务端设置</p>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-8"
                  disabled={!commandBusAvailable || act.isPending}
                  title={commandBusAvailable ? "重新加载受控端配置" : "没有可用的命令通道"}
                  onClick={() => act.mutate({ type: "reload_config" })}
                >
                  <RotateCcw className="size-3.5" aria-hidden />
                  重载
                </Button>
              </li>
              <li className="flex items-center justify-between gap-3 border-b border-border-soft px-6 py-4 last:border-0">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-foreground">重启受控端</p>
                  <p className="mt-1 text-xs text-muted-foreground">只重启服务，不会重启机器</p>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-8"
                  disabled={!commandBusAvailable || act.isPending}
                  title={commandBusAvailable ? "重启受控端服务（不会重启机器）" : "没有可用的命令通道"}
                  onClick={() => setPending("restart")}
                >
                  <Power className="size-3.5" aria-hidden />
                  重启
                </Button>
              </li>
              <li className="flex items-center justify-between gap-3 border-b border-border-soft px-6 py-4 last:border-0">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-foreground">远程卸载</p>
                  <p className={cn("mt-1 text-xs", capabilities.uninstall ? "text-muted-foreground" : "text-warning")}>
                    {capabilities.uninstall
                      ? "以 SYSTEM 权限自我清理，不弹 UAC"
                      : `当前不可卸载（${device.presence.reason}）`}
                  </p>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-8 text-destructive"
                  disabled={!capabilities.uninstall || uninstall.isPending}
                  title={capabilities.uninstall ? "以 SYSTEM 权限自我清理，不弹 UAC" : "设备当前不可下发卸载指令"}
                  onClick={() => setPending("uninstall")}
                >
                  <Unplug className="size-3.5" aria-hidden />
                  卸载
                </Button>
              </li>
            </ul>
          </Panel>
        </div>

        <div className="flex min-w-0 flex-col gap-6">
          <RenamePanel device={device} onSaved={onRefresh} />

          <Panel
            title="数据清理"
            tone="danger"
            hint="删除记录会同时清除本地画面与归档仓库中的文件。"
            flush
            className="min-w-0"
          >
            <ul className="flex flex-col">
              <li className="flex items-center justify-between gap-3 border-b border-border-soft px-6 py-4 last:border-0">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-foreground">清空历史画面</p>
                  <p className="mt-1 text-xs text-muted-foreground">保留设备与归档远端副本</p>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-8"
                  disabled={purgeFrames.isPending}
                  onClick={() => setPending("purge")}
                >
                  <Trash2 className="size-3.5" aria-hidden />
                  清空
                </Button>
              </li>
              <li className="flex items-center justify-between gap-3 border-b border-border-soft px-6 py-4 last:border-0">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-foreground">删除设备记录</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {canDeleteRecord ? "不可撤销；会清除画面与归档文件" : "设备仍有活动通道，需先远程卸载"}
                  </p>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-8 text-destructive"
                  disabled={remove.isPending || !canDeleteRecord}
                  title={canDeleteRecord ? "删除这台设备的历史记录" : "设备仍有活动通道，请先远程卸载"}
                  onClick={() => setPending("delete")}
                >
                  <Trash2 className="size-3.5" aria-hidden />
                  删除
                </Button>
              </li>
            </ul>
          </Panel>
        </div>
      </div>

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
