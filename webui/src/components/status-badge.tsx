/**
 * The only component that renders device liveness.
 *
 * It renders the server's verdict verbatim: `presence.state`, the transport and
 * the server's own `reason` / `age_ms`. It never invents a freshness heuristic.
 */

import { Badge } from "@/components/ui/badge";
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

const VARIANT: Record<
  Presence["state"],
  "success" | "warning" | "neutral" | "destructive"
> = {
  online: "success",
  degraded: "warning",
  offline: "neutral",
  uninstalling: "warning",
  uninstalled: "destructive",
};

const DOTS: Record<Presence["state"], string> = {
  online: "bg-success",
  degraded: "bg-warning",
  offline: "bg-faint",
  uninstalling: "bg-warning animate-sm-pulse",
  uninstalled: "bg-destructive",
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
    <Badge
      variant={VARIANT[presence.state]}
      className={cn(
        "max-w-full gap-1.5 font-medium",
        large ? "h-7 px-3 text-sm" : compact ? "h-5 px-2 text-xs" : null,
        className,
      )}
      title={presence.reason || label}
    >
      <span
        className={cn("shrink-0 rounded-full", large ? "size-2" : "size-1.5", DOTS[presence.state])}
        aria-hidden
      />
      <span className="truncate">{label}</span>
      {!compact && detail ? (
        <span className="truncate text-xs font-normal">· {detail}</span>
      ) : null}
      {!compact && age !== null && presence.state !== "online" ? (
        <span className="shrink-0 text-xs font-normal">· {formatAge(age)}</span>
      ) : null}
    </Badge>
  );
}
