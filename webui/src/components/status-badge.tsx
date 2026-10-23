/**
 * The only component that renders device liveness.
 *
 * It renders the server's verdict verbatim: `presence.state`, the transport and
 * the server's own `reason` / `age_ms`. It does not add a "stale" heuristic of
 * its own — the 0.2.x console did (`age > 120s → 状态未知`), which duplicated the
 * server threshold and, worse, judged it with the local clock (spec C5, A12).
 * When the console socket is down, the shell says so globally; the badge keeps
 * showing the last server statement, which is the truth as of that response.
 */

import { formatAge } from "@/lib/format";
import { presenceAgeMs, transportOf } from "@/lib/presence";
import type { Presence, TransportKind } from "@/lib/types";
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
  compact?: boolean;
  className?: string;
}

export function StatusBadge({ presence, compact = false, className }: Props) {
  const age = presenceAgeMs(presence);
  const transport = transportOf(presence);
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