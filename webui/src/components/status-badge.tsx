import { formatAge } from "@/lib/format";
import { transportOf } from "@/lib/presence";
import type { Presence, TransportKind } from "@/lib/types";
import { useAge } from "@/hooks/use-server-now";
import { cn } from "@/lib/utils";

const LABELS: Record<Presence["state"], string> = {
  online: "在线",
  degraded: "受限",
  offline: "离线",
  uninstalling: "卸载中",
  uninstalled: "已卸载",
};

const TONES: Record<Presence["state"], string> = {
  online: "bg-ok/12 text-ok border-ok/30",
  degraded: "bg-warn/12 text-warn border-warn/30",
  offline: "bg-sunken text-soft border-line",
  uninstalling: "bg-warn/12 text-warn border-warn/30",
  uninstalled: "bg-danger/12 text-danger border-danger/30",
};

const DOTS: Record<Presence["state"], string> = {
  online: "bg-ok",
  degraded: "bg-warn",
  offline: "bg-faint",
  uninstalling: "bg-warn animate-sm-pulse",
  uninstalled: "bg-danger",
};

const TRANSPORT_LABELS: Record<TransportKind, string> = {
  ws: "实时通道",
  poll: "轮询",
  none: "无通道",
};

interface Props {
  presence: Presence;
  /** True when the console's own event socket is down, so "now" is unreliable. */
  stale?: boolean;
  compact?: boolean;
  className?: string;
}

/**
 * The only component that renders device liveness.
 *
 * It shows the server's verdict verbatim, plus *why* when the state is not plain
 * "online". When the console's own socket is down and the device has not been heard
 * from recently, it says 状态未知 rather than inventing an answer.
 */
export function StatusBadge({ presence, stale = false, compact = false, className }: Props) {
  const age = useAge(presence.last_seen_ms);
  const transport = transportOf(presence);
  const unknown = stale && presence.state !== "uninstalled" && (age === null || age > 120_000);

  if (unknown) {
    return (
      <span
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border border-line bg-sunken px-2 py-0.5 text-xs font-medium text-soft",
          className,
        )}
        title="控制台与服务器的连接已断开，无法确认设备当前状态"
      >
        <span className="size-1.5 rounded-full bg-faint" />
        状态未知
      </span>
    );
  }

  const label = LABELS[presence.state];
  const detail =
    presence.state === "online"
      ? TRANSPORT_LABELS[transport]
      : transport === "poll"
        ? "仅轮询"
        : presence.reason;

  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium",
        TONES[presence.state],
        className,
      )}
      title={presence.reason || label}
    >
      <span className={cn("size-1.5 shrink-0 rounded-full", DOTS[presence.state])} />
      <span className="truncate">{label}</span>
      {!compact && detail ? (
        <span className="truncate text-[0.7rem] font-normal opacity-75">· {detail}</span>
      ) : null}
      {!compact && age !== null && presence.state !== "online" ? (
        <span className="shrink-0 text-[0.7rem] font-normal opacity-60">{formatAge(age)}</span>
      ) : null}
    </span>
  );
}