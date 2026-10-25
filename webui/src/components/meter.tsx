import { useViewport } from "@/hooks/use-viewport";
import { cn } from "@/lib/utils";

type Tone = "brand" | "ok" | "warn" | "danger";

const TONE_BAR: Record<Tone, string> = {
  brand: "bg-brand",
  ok: "bg-ok",
  warn: "bg-warn",
  danger: "bg-danger",
};

const TONE_STROKE: Record<Tone, string> = {
  brand: "var(--c-brand)",
  ok: "var(--c-ok)",
  warn: "var(--c-warn)",
  danger: "var(--c-danger)",
};

interface MeterProps {
  label: string;
  value: number | null | undefined;
  suffix?: string;
  tone?: Tone;
  compact?: boolean;
  hint?: string;
}

function toneFor(value: number): Tone {
  if (value >= 90) return "danger";
  if (value >= 75) return "warn";
  return "brand";
}

/** A labelled linear gauge. Deliberately not a chart: readable on a watch face. */
export function Meter({ label, value, suffix = "%", tone = "brand", compact = false, hint }: MeterProps) {
  const numeric = Number(value ?? 0);
  const safe = Number.isFinite(numeric) ? Math.max(0, Math.min(100, numeric)) : 0;
  const resolved: Tone = tone === "brand" ? toneFor(safe) : tone;

  return (
    <div className={cn("min-w-0", compact ? "space-y-1" : "space-y-1.5")}>
      <div className="flex min-w-0 items-baseline justify-between gap-2">
        <span className={cn("truncate text-soft", compact ? "text-[0.72rem]" : "text-[0.8rem]")}>{label}</span>
        <span
          className={cn(
            "tnum shrink-0 font-semibold text-ink",
            compact ? "text-[0.78rem]" : "text-[0.9rem]",
          )}
        >
          {value === null || value === undefined ? "—" : `${safe.toFixed(0)}${suffix}`}
        </span>
      </div>
      <div className={cn("w-full overflow-hidden rounded-full bg-sunken", compact ? "h-1" : "h-1.5")}>
        <div
          className={cn("h-full rounded-full transition-all", TONE_BAR[resolved])}
          style={{ width: `${safe}%` }}
        />
      </div>
      {hint ? <p className="truncate text-[0.68rem] text-faint">{hint}</p> : null}
    </div>
  );
}

/**
 * SVG radial gauge for the overview hero. Uses stroke-dasharray/dashoffset
 * (supported since Chrome 1) instead of conic-gradient masks.
 */
export function RingMeter({
  label,
  value,
  suffix = "%",
  size,
  tone = "brand",
}: {
  label: string;
  value: number | null | undefined;
  suffix?: string;
  size?: number;
  tone?: Tone;
}) {
  const viewport = useViewport();
  const resolvedSize = size ?? (viewport === "watch" ? 68 : viewport === "mobile" ? 84 : 94);
  const numeric = Number(value ?? 0);
  const safe = Number.isFinite(numeric) ? Math.max(0, Math.min(100, numeric)) : 0;
  const resolved: Tone = tone === "brand" ? toneFor(safe) : tone;
  const stroke = 7;
  const radius = (resolvedSize - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - safe / 100);

  return (
    <div className="flex min-w-0 flex-col items-center">
      <div className="relative" style={{ width: resolvedSize, height: resolvedSize }}>
        <svg
          width={resolvedSize}
          height={resolvedSize}
          viewBox={`0 0 ${resolvedSize} ${resolvedSize}`}
          aria-hidden="true"
          className="-rotate-90"
          style={{ transform: "rotate(-90deg)" }}
        >
          <circle
            cx={resolvedSize / 2}
            cy={resolvedSize / 2}
            r={radius}
            fill="none"
            stroke="var(--c-sunken-strong)"
            strokeWidth={stroke}
          />
          <circle
            cx={resolvedSize / 2}
            cy={resolvedSize / 2}
            r={radius}
            fill="none"
            stroke={TONE_STROKE[resolved]}
            strokeWidth={stroke}
            strokeLinecap="round"
            strokeDasharray={`${circumference} ${circumference}`}
            strokeDashoffset={offset}
            style={{ transition: "stroke-dashoffset 0.5s ease" }}
          />
        </svg>
        <span className="inset-full flex items-center justify-center">
          <span className="tnum text-center text-[0.9rem] font-semibold text-ink">
            {value === null || value === undefined ? "—" : `${safe.toFixed(0)}${suffix}`}
          </span>
        </span>
      </div>
      <span className="mt-1.5 truncate text-[0.72rem] text-soft">{label}</span>
    </div>
  );
}
