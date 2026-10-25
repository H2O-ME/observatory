import type { ComponentType, ReactNode } from "react";

import { useViewport } from "@/hooks/use-viewport";
import { cn } from "@/lib/utils";

interface SectionProps {
  children: ReactNode;
  title?: ReactNode;
  icon?: ComponentType<{ className?: string }>;
  hint?: ReactNode;
  actions?: ReactNode;
  tone?: "default" | "quiet" | "danger" | "warn" | "ok";
  className?: string;
  bodyClassName?: string;
}

/**
 * A titled instrument panel. Standardises padding and the title row across
 * every page; deliberately uses grid gap (not flex gap) for the watch.
 */
export function Section({
  children,
  title,
  icon: Icon,
  hint,
  actions,
  tone = "default",
  className,
  bodyClassName,
}: SectionProps) {
  const viewport = useViewport();
  const watch = viewport === "watch";
  const surface =
    tone === "danger"
      ? "surface-danger"
      : tone === "warn"
        ? "surface-warn"
        : tone === "ok"
          ? "surface-ok"
          : tone === "quiet"
            ? "surface-quiet"
            : "surface";

  return (
    <section className={cn(surface, "min-w-0", watch ? "p-3" : "p-4 sm:p-5", className)}>
      {title || actions ? (
        <div className="mb-3 flex min-w-0 flex-wrap items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            {Icon ? (
              <span className="flex size-7 shrink-0 items-center justify-center rounded-lg border border-line bg-sunken text-soft">
                <Icon className="size-3.5" />
              </span>
            ) : null}
            <div className="min-w-0">
              <h2
                className={cn(
                  "min-w-0 font-semibold tracking-tight text-ink",
                  watch ? "text-[0.82rem]" : "text-[0.95rem]",
                )}
              >
                {title}
              </h2>
              {hint ? (
                <p className={cn("mt-0.5 leading-relaxed text-faint", watch ? "text-[0.68rem]" : "text-[0.74rem]")}>
                  {hint}
                </p>
              ) : null}
            </div>
          </div>
          {actions ? <div className="flex shrink-0 flex-wrap items-center gap-1.5">{actions}</div> : null}
        </div>
      ) : null}
      <div className={cn("min-w-0", bodyClassName)}>{children}</div>
    </section>
  );
}

export function SectionHeader({
  title,
  hint,
  icon: Icon,
  actions,
  className,
}: {
  title: ReactNode;
  hint?: ReactNode;
  icon?: ComponentType<{ className?: string }>;
  actions?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex min-w-0 flex-wrap items-center justify-between gap-2", className)}>
      <div className="flex min-w-0 items-center gap-2">
        {Icon ? (
          <span className="flex size-7 shrink-0 items-center justify-center rounded-lg border border-line bg-sunken text-soft">
            <Icon className="size-3.5" />
          </span>
        ) : null}
        <div className="min-w-0">
          <h2 className="min-w-0 text-[0.95rem] font-semibold tracking-tight text-ink">{title}</h2>
          {hint ? <p className="mt-0.5 text-[0.74rem] leading-relaxed text-faint">{hint}</p> : null}
        </div>
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-1.5">{actions}</div> : null}
    </div>
  );
}
