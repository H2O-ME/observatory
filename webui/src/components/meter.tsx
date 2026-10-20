import { cn } from "@/lib/utils";

interface Props {
  label: string;
  value: number | null | undefined;
  suffix?: string;
  tone?: "brand" | "ok" | "warn" | "danger";
  compact?: boolean;
}

const TONE_CLASS: Record<NonNullable<Props["tone"]>, string> = {
  brand: "bg-brand",
  ok: "bg-ok",
  warn: "bg-warn",
  danger: "bg-danger",
};

/**
 * A tiny bar for CPU / memory / disk.
 *
 * Deliberately not a chart: on a 320px screen an SVG chart is noise. A labelled bar
 * with one number is readable at a glance, which is the point of the wall view.
 */
export function Meter({ label, value, suffix = "%", tone = "brand", compact = false }: Props) {
  const numeric = Number(value ?? 0);
  const safe = Number.isFinite(numeric) ? Math.max(0, Math.min(100, numeric)) : 0;
  const resolvedTone: NonNullable<Props["tone"]> =
    tone === "brand" ? (safe >= 90 ? "danger" : safe >= 75 ? "warn" : "brand") : tone;

  return (
    <div className={cn("min-w-0", compact ? "space-y-1" : "space-y-1.5")}>
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate text-xs text-soft">{label}</span>
        <span className={cn("shrink-0 tabular-nums", compact ? "text-xs" : "text-sm", "font-medium text-ink")}>
          {value === null || value === undefined ? "—" : `${safe.toFixed(0)}${suffix}`}
        </span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-sunken">
        <div
          className={cn("h-full rounded-full transition-all", TONE_CLASS[resolvedTone])}
          style={{ width: `${safe}%` }}
        />
      </div>
    </div>
  );
}