import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  Activity,
  Camera,
  CheckCircle2,
  HardDrive,
  Info,
  PencilLine,
  Power,
  RadioTower,
  RotateCcw,
  Trash2,
  TriangleAlert,
  Unplug,
  Users,
} from "lucide-react";

import { Meter, RingMeter } from "@/components/meter";
import { Banner } from "@/components/async-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { Section } from "@/components/section";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
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

/**
 * Rename form.
 *
 * Local state is seeded from the server value and only re-synced while the form
 * is clean, so a background refetch cannot overwrite what the operator is
 * typing, and switching to another device never inherits the previous name.
 */
function RenameCard({ device, onSaved }: { device: Device; onSaved: () => void }) {
  const viewport = useViewport();
  const watch = viewport === "watch";
  const desktop = viewport === "desktop";
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

  const bodyText = watch ? "text-[0.78rem]" : "text-[0.85rem]";

  return (
    <Section
      title="重命名"
      icon={PencilLine}
      actions={dirty ? <span className="eyebrow text-warn">未保存</span> : null}
      className="min-w-0 xl:col-span-1"
    >
      <div className="space-y-2.5">
        <div className="space-y-1">
          <Label htmlFor="device-name" className={cn("text-faint", bodyText)}>
            显示名称
          </Label>
          <Input
            id="device-name"
            value={name}
            onChange={(event) => {
              setDirty(true);
              setName(event.target.value);
            }}
            className={desktop ? "h-9" : "h-11"}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="device-note" className={cn("text-faint", bodyText)}>
            备注
          </Label>
          <Input
            id="device-note"
            value={note}
            onChange={(event) => {
              setDirty(true);
              setNote(event.target.value);
            }}
            className={desktop ? "h-9" : "h-11"}
          />
        </div>
        <div className="flex flex-wrap items-center">
          <Button
            size="sm"
            className={cn("mr-2", !desktop && "tap")}
            onClick={() => save.mutate()}
            disabled={save.isPending}
          >
            保存
          </Button>
          {save.isError ? (
            <span className={cn("text-danger", bodyText)}>{errorText(save.error, "保存失败")}</span>
          ) : null}
        </div>
      </div>
    </Section>
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
  const desktop = viewport === "desktop";
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

  const bodyText = watch ? "text-[0.78rem]" : "text-[0.85rem]";
  const metaText = watch ? "text-[0.7rem]" : "text-[0.76rem]";
  const gridGap = watch ? "gap-2" : "gap-3 sm:gap-4";

  // Telemetry bento: the resource card always exists; disks / sessions join it
  // when the server reports them, and the spans rebalance so no column is left
  // as dead space on a wide desktop.
  const telemetryCount = 1 + (disks.length > 0 ? 1 : 0) + (sessions.length > 0 ? 1 : 0);
  const resourceSpan = telemetryCount === 1 ? "xl:col-span-4" : "xl:col-span-2";
  const secondarySpan = telemetryCount === 2 ? "xl:col-span-2" : "xl:col-span-1";

  return (
    <div className={cn("min-w-0", watch ? "space-y-2.5" : "space-y-4 sm:space-y-5")}>
      {message ? (
        <Banner tone="ok" icon={CheckCircle2} className="py-2">
          {message}
        </Banner>
      ) : null}
      {error ? (
        <Banner tone="error" icon={TriangleAlert} className="py-2">
          {error}
        </Banner>
      ) : null}

      {captureBlocked ? (
        <Banner tone="warn" icon={Camera} title="无法采集屏幕" className="py-2.5">
          {captureReasonIsNew ? <p>{captureReason}</p> : null}
          <p className={cn("text-faint", metaText, captureReasonIsNew && "mt-1")}>
            受控端服务运行在会话 0，需要保持桌面会话登录；登录后画面会自动恢复。
          </p>
        </Banner>
      ) : null}

      <div className={cn("grid min-w-0 md:grid-cols-2 xl:grid-cols-4", gridGap)}>
        <Section
          title="资源占用"
          icon={Activity}
          className={cn("min-w-0 md:col-span-2", resourceSpan)}
        >
          <div className="grid grid-cols-2 gap-2 sm:gap-3">
            <RingMeter label="CPU" value={cpu} />
            <RingMeter label="内存" value={memory} />
          </div>
          <dl
            className={cn(
              "mt-3 grid grid-cols-2 gap-x-3 gap-y-1.5",
              watch ? "text-[0.72rem]" : "text-[0.78rem]",
            )}
          >
            <div className="min-w-0">
              <dt className="text-faint">内存总量</dt>
              <dd className="tnum truncate text-soft" title={formatBytes(memTotal)}>
                {formatBytes(memTotal)}
              </dd>
            </div>
            <div className="min-w-0">
              <dt className="text-faint">运行时长</dt>
              <dd className="truncate text-soft" title={formatDuration(num(device.sysinfo, "uptime_s"))}>
                {formatDuration(num(device.sysinfo, "uptime_s"))}
              </dd>
            </div>
            <div className="min-w-0">
              <dt className="text-faint">处理器</dt>
              <dd className="truncate text-soft" title={text(device.sysinfo, "cpu_model") || "—"}>
                {text(device.sysinfo, "cpu_model") || "—"}
              </dd>
            </div>
            <div className="min-w-0">
              <dt className="text-faint">当前用户</dt>
              <dd className="truncate text-soft" title={text(device.sysinfo, "username") || "—"}>
                {text(device.sysinfo, "username") || "—"}
              </dd>
            </div>
          </dl>
        </Section>

        {disks.length > 0 ? (
          <Section title="磁盘" icon={HardDrive} className={cn("min-w-0", secondarySpan)}>
            <div className="space-y-2.5">
              {disks.map((disk, index) => {
                // Bar length = 已用占比, derived from the server's total/free
                // pair (fall back to the agent's own percent when totals are
                // missing). High fill therefore reads as "almost full" — the
                // same danger direction as CPU/内存, not the inverse.
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
          </Section>
        ) : null}

        {sessions.length > 0 ? (
          <Section title="登录会话" icon={Users} className={cn("min-w-0", secondarySpan)}>
            <ul className="space-y-1.5">
              {sessions.map((session) => (
                <li
                  key={session.session_id}
                  className={cn(
                    "grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2 text-soft",
                    bodyText,
                  )}
                >
                  <span className="min-w-0 truncate">
                    会话 {session.session_id} · {session.username || "（未登录）"}
                  </span>
                  <span className={cn("shrink-0 text-faint", metaText)}>{session.state}</span>
                </li>
              ))}
            </ul>
          </Section>
        ) : null}
      </div>

      <div className={cn("grid min-w-0 md:grid-cols-2 xl:grid-cols-3", gridGap)}>
        <Section title="设备信息" icon={Info} className="min-w-0 xl:col-span-2">
          <dl
            className={cn(
              "grid grid-cols-[4.5rem_minmax(0,1fr)] gap-x-3 gap-y-1.5",
              bodyText,
            )}
          >
            <dt className="text-faint">设备标识</dt>
            <dd className="tnum truncate text-soft" title={device.uid}>
              {device.uid}
            </dd>
            <dt className="text-faint">系统</dt>
            <dd className="truncate text-soft" title={`${device.os_name} ${device.os_version}`}>
              {device.os_name} {device.os_version}
            </dd>
            <dt className="text-faint">架构</dt>
            <dd className="truncate text-soft">{device.arch}</dd>
            <dt className="text-faint">受控端</dt>
            <dd className="tnum truncate text-soft">
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
        </Section>

        <RenameCard device={device} onSaved={onRefresh} />
      </div>

      {selfUpdateBlocked ? (
        <Banner tone="warn" icon={TriangleAlert} title="无法自动升级" className="py-2.5">
          <p>{selfUpdateBlocked}</p>
          <p className={cn("mt-1 text-faint", metaText)}>
            该版本不在自动更新路径上，服务器不会向它推送新版本。请在目标机器上重新运行最新的安装脚本（覆盖安装，设备标识与历史记录都会保留）。
          </p>
        </Banner>
      ) : null}

      <div className={cn("grid min-w-0 md:grid-cols-2", gridGap)}>
        <Section title="远程操作" icon={RadioTower} className="min-w-0">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <Button
              size="sm"
              variant="default"
              className={cn("w-full justify-center", !desktop && "tap")}
              disabled={!capabilities.screenshot || act.isPending}
              title={capabilities.screenshot ? "让受控端立即截一张图" : "受控端当前无法截图"}
              onClick={() => act.mutate({ type: "screenshot" })}
            >
              <Camera className="size-3.5" aria-hidden />
              立即截图
            </Button>
            <Button
              size="sm"
              variant="outline"
              className={cn("w-full justify-center", !desktop && "tap")}
              disabled={!commandBusAvailable || act.isPending}
              title={commandBusAvailable ? "重新加载受控端配置" : "没有可用的命令通道"}
              onClick={() => act.mutate({ type: "reload_config" })}
            >
              <RotateCcw className="size-3.5" aria-hidden />
              重新加载配置
            </Button>
            <Button
              size="sm"
              variant="outline"
              className={cn("w-full justify-center", !desktop && "tap")}
              disabled={!commandBusAvailable || act.isPending}
              title={commandBusAvailable ? "重启受控端服务（不会重启机器）" : "没有可用的命令通道"}
              onClick={() => setPending("restart")}
            >
              <Power className="size-3.5" aria-hidden />
              重启受控端
            </Button>
            <Button
              size="sm"
              variant="destructive"
              className={cn("w-full justify-center", !desktop && "tap")}
              disabled={!capabilities.uninstall || uninstall.isPending}
              title={capabilities.uninstall ? "以 SYSTEM 权限自我清理，不弹 UAC" : "设备当前不可下发卸载指令"}
              onClick={() => setPending("uninstall")}
            >
              <Unplug className="size-3.5" aria-hidden />
              远程卸载
            </Button>
          </div>
          <p
            className={cn(
              "mt-2.5 leading-relaxed",
              metaText,
              capabilities.uninstall ? "text-faint" : "text-warn",
            )}
          >
            {capabilities.uninstall
              ? "远程卸载不弹 UAC：受控端服务本身以 SYSTEM 运行，会自行停止、删除并使记录标记为已卸载。"
              : `设备当前不可下发卸载指令（${device.presence.reason}）`}
          </p>
        </Section>

        <Section tone="danger" title="数据清理" icon={Trash2} className="min-w-0">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <Button
              size="sm"
              variant="outline"
              className={cn("w-full justify-center", !desktop && "tap")}
              disabled={purgeFrames.isPending}
              onClick={() => setPending("purge")}
            >
              <Trash2 className="size-3.5" aria-hidden />
              清空历史画面
            </Button>
            <Button
              size="sm"
              variant="destructive"
              className={cn("w-full justify-center", !desktop && "tap")}
              disabled={remove.isPending || !canDeleteRecord}
              title={canDeleteRecord ? "删除这台设备的历史记录" : "设备仍有活动通道，请先远程卸载"}
              onClick={() => setPending("delete")}
            >
              <Trash2 className="size-3.5" aria-hidden />
              删除设备记录
            </Button>
          </div>
          <Separator className="my-2.5" />
          <p className={cn("leading-relaxed text-faint", bodyText)}>
            删除记录会同时清除本地画面与该设备在归档仓库中的文件；在线设备必须先远程卸载，避免留下仍会尝试上报的机器。
          </p>
        </Section>
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
