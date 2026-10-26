import type { ComponentType, ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { buttonVariants } from "@/components/ui/button";
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
  /** Accepted for compatibility; page headers carry no icon tile. */
  icon?: ComponentType<{ className?: string }>;
  /** Back link rendered before the title (device pages). */
  backTo?: string;
  backLabel?: string;
  /** Chips / meta line under the description. */
  meta?: ReactNode;
  className?: string;
}

/**
 * The one page header.
 *
 * Kicker (optional) → title → description → meta, with actions opposite. A
 * single hairline closes the block so the first panel below starts a new
 * section; pages do not need to invent their own divider.
 */
export function PageHeader({
  title,
  eyebrow,
  description,
  subtitle,
  actions,
  backTo,
  backLabel = "返回",
  meta,
  className,
}: PageHeaderProps) {
  const detail = description ?? subtitle;

  return (
    <header className={cn("w-full min-w-0 border-b border-border pb-4 sm:pb-5", className)}>
      <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
        <div className="flex min-w-0 items-start gap-3">
          {backTo ? (
            <Link
              to={backTo}
              aria-label={backLabel}
              className={cn(buttonVariants({ variant: "outline", size: "icon-sm" }), "mt-0.5 shrink-0 no-underline")}
            >
              <ArrowLeft className="size-4" />
            </Link>
          ) : null}

          <div className="min-w-0 flex-1">
            {eyebrow ? <p className="eyebrow mb-2">{eyebrow}</p> : null}
            <h1 className="min-w-0 text-xl leading-tight font-semibold tracking-[-0.025em] text-foreground sm:text-2xl">
              {title}
            </h1>
            {detail ? (
              <div className="mt-1.5 max-w-2xl text-sm leading-relaxed text-muted-foreground">
                {detail}
              </div>
            ) : null}
            {meta ? <div className="mt-2.5 flex flex-wrap items-center gap-1.5">{meta}</div> : null}
          </div>
        </div>

        {actions ? (
          <div className="flex shrink-0 flex-wrap items-center gap-2 sm:justify-end">
            {actions}
          </div>
        ) : null}
      </div>
    </header>
  );
}

/** Small metadata chip used by PageHeader.meta and panels. */
export function MetaChip({
  children,
  tone = "default",
  className,
}: {
  children: ReactNode;
  tone?: "default" | "ok" | "warn" | "danger" | "brand";
  className?: string;
}) {
  const variant =
    tone === "ok"
      ? "success"
      : tone === "warn"
        ? "warning"
        : tone === "danger"
          ? "destructive"
          : tone === "brand"
            ? "outline"
            : "neutral";

  return (
    <Badge variant={variant} className={cn("gap-1.5", className)}>
      {children}
    </Badge>
  );
}
