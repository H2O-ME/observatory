/**
 * The only component that renders device liveness.
 *
 * It renders the server's verdict verbatim: `presence.state`, the transport and
 * the server's own `reason` / `age_ms`. It never invents a freshness heuristic.
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
  online: "border-line bg-ok-soft text-ok",
  degraded: "border-line bg-warn-soft text-warn",
  offline: "border-line bg-sunken text-soft",
  uninstalling: "border-line bg-warn-soft text-warn",
  uninstalled: "border-line bg-danger-soft text-danger",
};

const DOTS: Record<Presence["state"], string> = {
  online: "bg-ok shadow-[0_0_0_3px_var(--c-ok-soft)]",
  degraded: "bg-warn shadow-[0_0_0_3px_var(--c-warn-soft)]",
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
  /** Render as a larger dot + label pill (device hero). */
  large?: boolean;
}

export function StatusBadge({ presence, compact = false, className, large = false }: Props) {
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
        "inline-flex max-w-full items-center gap-1.5 rounded-full border font-medium",
        compact ? "px-2 py-0.5 text-[0.72rem]" : "px-2.5 py-1 text-[0.78rem]",
        large && "px-3 py-1.5 text-[0.84rem]",
        TONES[presence.state],
        className,
      )}
      title={presence.reason || label}
    >
      <span
        className={cn(
          "shrink-0 rounded-full",
          large ? "size-2" : "size-1.5",
          DOTS[presence.state],
        )}
      />
      <span className="truncate">{label}</span>
      {!compact && detail ? (
        <span className="truncate text-[0.72rem] font-normal opacity-80">· {detail}</span>
      ) : null}
      {!compact && age !== null && presence.state !== "online" ? (
        <span className="shrink-0 text-[0.7rem] font-normal opacity-65">{formatAge(age)}</span>
      ) : null}
    </span>
  );
}
