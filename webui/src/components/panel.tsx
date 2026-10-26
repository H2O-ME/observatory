import type { ReactNode } from "react";

import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

interface PanelProps {
  title?: ReactNode;
  hint?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  /** Lists and tables bring their own row padding. */
  flush?: boolean;
  tone?: "default" | "danger";
}

/**
 * The one content container.
 *
 * A panel is a single hairline rectangle with a header rule — not a card inside
 * a card. Pages group 2–5 of these; anything that is really a list stays a list
 * inside the panel instead of becoming a stack of tinted boxes.
 */
export function Panel({
  title,
  hint,
  actions,
  children,
  className,
  bodyClassName,
  flush = false,
  tone = "default",
}: PanelProps) {
  return (
    <Card className={cn("min-w-0 gap-0 overflow-hidden rounded-2xl py-0", className)}>
      {title || actions ? (
        <header className="flex min-w-0 items-center justify-between gap-3 px-6 pt-6 pb-3">
          <div className="min-w-0">
            <h2
              className={cn(
                "min-w-0 truncate text-base leading-snug font-semibold text-foreground",
                tone === "danger" && "text-destructive",
              )}
            >
              {title}
            </h2>
            {hint ? (
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{hint}</p>
            ) : null}
          </div>
          {actions ? (
            <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">
              {actions}
            </div>
          ) : null}
        </header>
      ) : null}
      <div className={cn("min-w-0", flush ? null : "p-6", bodyClassName)}>{children}</div>
    </Card>
  );
}

/** Label/value line used inside panels (device info, server state, sessions). */
export function PanelRow({
  label,
  children,
  className,
}: {
  label: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex min-w-0 items-center gap-4 text-sm leading-relaxed", className)}>
      <dt className="w-20 shrink-0 truncate text-sm text-muted-foreground">{label}</dt>
      <dd className="min-w-0 flex-1 truncate text-sm text-foreground">{children}</dd>
    </div>
  );
}
