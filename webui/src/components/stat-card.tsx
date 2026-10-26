import type { ComponentType, ReactNode } from "react";

import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

type Tone = "brand" | "ok" | "warn" | "danger" | "neutral";

const VALUE_TONE: Record<Tone, string> = {
  brand: "text-foreground",
  ok: "text-foreground",
  warn: "text-warning",
  danger: "text-destructive",
  neutral: "text-foreground",
};

/**
 * Fleet/server metric strip.
 *
 * One framed row with internal rules instead of four free-floating cards. The
 * label is the quiet element and the number carries the weight; status colour
 * appears only when the value itself is a warning.
 */
export function StatStrip({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <Card
      className={cn(
        "stat-strip grid min-w-0 gap-0 overflow-hidden rounded-2xl border-0 bg-card py-0 shadow-sm",
        className,
      )}
    >
      {children}
    </Card>
  );
}

export function StatCell({
  label,
  value,
  hint,
  tone = "neutral",
  className,
}: {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone;
  className?: string;
}) {
  return (
    <div className={cn("flex min-w-0 flex-col px-6 py-6", className)}>
      <p className="mb-1 truncate text-left text-sm leading-5 text-muted-foreground">{label}</p>
      <p
        key={String(value)}
        className={cn(
          "tnum animate-sm-fade truncate text-left text-2xl leading-8 font-semibold tracking-tight",
          VALUE_TONE[tone],
        )}
      >
        {value}
      </p>
      {hint ? <p className="mt-1 truncate text-left text-xs leading-relaxed text-faint">{hint}</p> : null}
    </div>
  );
}

interface StatCardProps {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  icon?: ComponentType<{ className?: string }>;
  tone?: Tone;
  className?: string;
  onClick?: () => void;
}

/** One KPI card (used in the 12-column device grid). */
export function StatCard({
  label,
  value,
  hint,
  icon: Icon,
  tone = "neutral",
  className,
  onClick,
}: StatCardProps) {
  return (
    <Card
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={
        onClick
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onClick();
              }
            }
          : undefined
      }
      className={cn(
        "h-full min-w-0 gap-0 py-0 transition-colors",
        onClick && "cursor-pointer hover:border-border-strong",
        className,
      )}
    >
      <div className="flex h-full min-w-0 flex-col px-6 py-6">
        <p className="mb-1 flex items-center gap-1.5 truncate text-left text-sm leading-5 text-muted-foreground">
          {Icon ? <Icon className="size-3.5 shrink-0" aria-hidden /> : null}
          {label}
        </p>
        <p
          key={String(value)}
          className={cn(
            "tnum animate-sm-fade truncate text-left text-2xl leading-8 font-semibold tracking-tight",
            VALUE_TONE[tone],
          )}
        >
          {value}
        </p>
        {hint ? (
          <p className="mt-1 truncate text-left text-xs leading-relaxed text-faint">{hint}</p>
        ) : null}
      </div>
    </Card>
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
