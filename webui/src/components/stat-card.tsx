import type { ComponentType, ReactNode } from "react";

import { useViewport } from "@/hooks/use-viewport";
import { cn } from "@/lib/utils";

type Tone = "brand" | "ok" | "warn" | "danger" | "neutral";

const TONE_TEXT: Record<Tone, string> = {
  brand: "text-brand",
  ok: "text-ok",
  warn: "text-warn",
  danger: "text-danger",
  neutral: "text-ink",
};

const TONE_SOFT: Record<Tone, string> = {
  brand: "bg-brand-soft",
  ok: "bg-ok-soft",
  warn: "bg-warn-soft",
  danger: "bg-danger-soft",
  neutral: "bg-sunken",
};

interface StatCardProps {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  icon?: ComponentType<{ className?: string }>;
  tone?: Tone;
  className?: string;
  onClick?: () => void;
  /** Small trailing element, e.g. a trend tag. */
  aside?: ReactNode;
}

/**
 * Bento-style telemetry tile. Uses `.surface` rather than the generic Card so
 * it stays legible when backdrop-filter is dropped on the watch.
 */
export function StatCard({
  label,
  value,
  hint,
  icon: Icon,
  tone = "neutral",
  className,
  onClick,
  aside,
}: StatCardProps) {
  const viewport = useViewport();
  const watch = viewport === "watch";
  const Tag = onClick ? "button" : "div";

  return (
    <Tag
      type={onClick ? "button" : undefined}
      onClick={onClick}
      className={cn(
        "surface surface-hover group relative min-w-0 overflow-hidden text-left",
        watch ? "p-2.5" : "p-3.5 sm:p-4",
        onClick && "cursor-pointer",
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "pointer-events-none absolute -right-6 -top-8 size-24 rounded-full opacity-40 blur-2xl",
          TONE_SOFT[tone],
        )}
      />
      <div className="relative flex min-w-0 items-start justify-between gap-2">
        <div className={cn("min-w-0", watch ? "flex-1" : "")}>
          <p className={cn("eyebrow truncate", watch && "text-[0.6rem]")}>{label}</p>
          <p
            className={cn(
              "tnum mt-1 truncate font-semibold tracking-tight",
              TONE_TEXT[tone],
              watch ? "text-lg" : "text-[1.55rem] leading-tight",
            )}
          >
            {value}
          </p>
          {hint ? (
            <p className={cn("mt-1 truncate text-faint", watch ? "text-[0.65rem]" : "text-[0.72rem]")}>{hint}</p>
          ) : null}
        </div>
        {Icon ? (
          <span
            className={cn(
              "flex shrink-0 items-center justify-center rounded-xl border border-line",
              TONE_SOFT[tone],
              TONE_TEXT[tone],
              watch ? "size-7" : "size-9",
            )}
          >
            <Icon className={watch ? "size-3.5" : "size-4"} />
          </span>
        ) : aside ? (
          <span className="shrink-0">{aside}</span>
        ) : null}
      </div>
    </Tag>
  );
}

export function StatGrid({
  children,
  columns = 4,
  className,
}: {
  children: ReactNode;
  columns?: 2 | 3 | 4;
  className?: string;
}) {
  const cols =
    columns === 2
      ? "grid-cols-2"
      : columns === 3
        ? "grid-cols-2 md:grid-cols-3"
        : "grid-cols-2 md:grid-cols-4";
  return <div className={cn("grid min-w-0 gap-2 sm:gap-3", cols, className)}>{children}</div>;
}
