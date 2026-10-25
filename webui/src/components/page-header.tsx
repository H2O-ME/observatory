import type { ComponentType, ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";

import { useViewport } from "@/hooks/use-viewport";
import { cn } from "@/lib/utils";

interface PageHeaderProps {
  title: ReactNode;
  /** Small uppercase kicker, e.g. FLEET / MISSION CONTROL. */
  eyebrow?: ReactNode;
  description?: ReactNode;
  /** Back-compat alias for `description` (older pages used `subtitle`). */
  subtitle?: ReactNode;
  /** Right-aligned actions; they wrap below the title on narrow screens. */
  actions?: ReactNode;
  /** Optional leading icon (lucide component or any node). */
  icon?: ComponentType<{ className?: string }>;
  /** Back link rendered before the title (device pages). */
  backTo?: string;
  backLabel?: string;
  /** Chips / meta line under the description. */
  meta?: ReactNode;
  className?: string;
}

/**
 * The one page header. Hierarchy is deliberately strong: eyebrow → display
 * title → description → meta, with actions on the right on desktop and below
 * the title on phones/watches.
 *
 * It does not use flex `gap` for the title/action split (Chromium 81 lacks
 * flex gap); margins + grid are used instead.
 */
export function PageHeader({
  title,
  eyebrow,
  description,
  subtitle,
  actions,
  icon: Icon,
  backTo,
  backLabel = "返回",
  meta,
  className,
}: PageHeaderProps) {
  const viewport = useViewport();
  const watch = viewport === "watch";
  const detail = description ?? subtitle;

  return (
    <header className={cn("w-full min-w-0", className)}>
      <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          {backTo ? (
            <Link
              to={backTo}
              aria-label={backLabel}
              className="tap mt-0.5 inline-flex shrink-0 items-center justify-center rounded-xl border border-line bg-sunken text-soft no-underline transition-colors hover:text-ink"
              style={{ width: watch ? 40 : 44, height: watch ? 40 : 44 }}
            >
              <ArrowLeft className={watch ? "size-4" : "size-5"} />
            </Link>
          ) : null}

          {Icon && !backTo ? (
            <span
              className={cn(
                "mt-0.5 flex shrink-0 items-center justify-center rounded-2xl border border-line bg-sunken text-brand",
                watch ? "size-9" : "size-11",
              )}
            >
              <Icon className={watch ? "size-4" : "size-5"} />
            </span>
          ) : null}

          <div className="min-w-0 flex-1">
            {eyebrow ? <p className="eyebrow mb-1.5">{eyebrow}</p> : null}
            <h1
              className={cn(
                "min-w-0 font-semibold tracking-tight text-ink",
                watch ? "text-[1.05rem] leading-tight" : "text-[1.55rem] leading-tight sm:text-[1.8rem]",
              )}
            >
              {title}
            </h1>
            {detail ? (
              <div
                className={cn(
                  "mt-1.5 max-w-3xl leading-relaxed text-soft",
                  watch ? "text-[0.72rem]" : "text-[0.85rem]",
                )}
              >
                {detail}
              </div>
            ) : null}
            {meta ? <div className="mt-2.5 flex flex-wrap items-center gap-1.5">{meta}</div> : null}
          </div>
        </div>

        {actions ? (
          <div
            className={cn(
              "flex shrink-0 flex-wrap items-center gap-2",
              watch ? "w-full" : "sm:justify-end",
            )}
          >
            {actions}
          </div>
        ) : null}
      </div>
      <div className="mt-4 h-px w-full bg-gradient-to-r from-transparent via-line-strong to-transparent sm:mt-5" />
    </header>
  );
}

/** Small metadata chip used by PageHeader.meta and cards. */
export function MetaChip({
  children,
  tone = "default",
  className,
}: {
  children: ReactNode;
  tone?: "default" | "ok" | "warn" | "danger" | "brand";
  className?: string;
}) {
  const tones: Record<string, string> = {
    default: "border-line bg-sunken text-soft",
    brand: "border-line bg-brand-soft text-brand",
    ok: "border-line bg-ok-soft text-ok",
    warn: "border-line bg-warn-soft text-warn",
    danger: "border-line bg-danger-soft text-danger",
  };
  return (
    <span
      className={cn(
        "inline-flex max-w-full items-center gap-1.5 rounded-full border px-2.5 py-1 text-[0.72rem] font-medium",
        tones[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
