import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";

type Tone = "brand" | "ok" | "warn" | "danger";

const TONE_BAR: Record<Tone, string> = {
  brand: "bg-primary",
  ok: "bg-success",
  warn: "bg-warning",
  danger: "bg-destructive",
};

const TONE_STROKE: Record<Tone, string> = {
  brand: "var(--c-primary)",
  ok: "var(--c-success)",
  warn: "var(--c-warning)",
  danger: "var(--c-destructive)",
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

/**
 * A labelled linear gauge built on the shared Progress primitive. Tone is
 * chosen from the value by default (>=75 warning, >=90 destructive) so the
 * operator sees pressure without reading the number.
 */
export function Meter({ label, value, suffix = "%", tone = "brand", compact = false, hint }: MeterProps) {
  const numeric = Number(value ?? 0);
  const safe = Number.isFinite(numeric) ? Math.max(0, Math.min(100, numeric)) : 0;
  const resolved: Tone = tone === "brand" ? toneFor(safe) : tone;

  return (
    <div className="w-full min-w-0">
      <div className="flex w-full min-w-0 items-baseline justify-between gap-2">
        <span className={cn("truncate text-muted-foreground", compact ? "text-xs" : "text-sm")}>
          {label}
        </span>
        <span
          className={cn(
            "tnum shrink-0 font-semibold text-foreground",
            compact ? "text-xs" : "text-sm",
          )}
        >
          {value === null || value === undefined ? "—" : `${safe.toFixed(0)}${suffix}`}
        </span>
      </div>
      <Progress
        value={safe}
        aria-label={label}
        className={compact ? "mt-1" : "mt-1.5"}
        trackClassName={compact ? "h-1" : "h-1.5"}
        indicatorClassName={TONE_BAR[resolved]}
      />
      {hint ? (
        <p className="mt-2 truncate text-xs text-muted-foreground">{hint}</p>
      ) : null}
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
  size = 92,
  stroke = 7,
  tone = "brand",
  caption,
  showValue = true,
  showLabel = true,
}: {
  label: string;
  value: number | null | undefined;
  suffix?: string;
  size?: number;
  stroke?: number;
  tone?: Tone;
  caption?: string;
  showValue?: boolean;
  showLabel?: boolean;
}) {
  const numeric = Number(value ?? 0);
  const safe = Number.isFinite(numeric) ? Math.max(0, Math.min(100, numeric)) : 0;
  const resolved: Tone = tone === "brand" ? toneFor(safe) : tone;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - safe / 100);

  return (
    <div className="flex min-w-0 flex-col items-center">
      <div className="relative shrink-0" style={{ width: size, height: size }}>
        <svg
          width={size}
          height={size}
          viewBox={`0 0 ${size} ${size}`}
          aria-hidden="true"
          style={{ transform: "rotate(-90deg)" }}
        >
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke="var(--c-muted)"
            strokeWidth={stroke}
          />
          <circle
            cx={size / 2}
            cy={size / 2}
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
        {showValue ? (
          <span className="absolute-fill flex items-center justify-center">
            <span className="tnum text-center text-xl font-semibold tracking-[-0.02em] text-foreground">
              {value === null || value === undefined ? "—" : `${safe.toFixed(0)}${suffix}`}
            </span>
          </span>
        ) : null}
      </div>
      {showLabel ? (
        <span className="mt-2 w-full truncate text-center text-xs text-muted-foreground">{label}</span>
      ) : null}
      {caption ? (
        <span className="tnum mt-1 w-full truncate text-center text-xs font-medium text-muted-foreground">
          {caption}
        </span>
      ) : null}
    </div>
  );
}
