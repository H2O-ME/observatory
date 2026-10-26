import type { ComponentType, ReactNode } from "react";

import { Panel } from "@/components/panel";
import { cn } from "@/lib/utils";

interface SectionProps {
  children: ReactNode;
  title?: ReactNode;
  /** Kept so existing call sites can pass an icon without rendering a tile. */
  icon?: ComponentType<{ className?: string }>;
  hint?: ReactNode;
  actions?: ReactNode;
  tone?: "default" | "quiet" | "danger" | "warn" | "ok";
  className?: string;
  bodyClassName?: string;
  flush?: boolean;
}

const TONE: Record<NonNullable<SectionProps["tone"]>, "default" | "danger"> = {
  default: "default",
  quiet: "default",
  danger: "danger",
  warn: "default",
  ok: "default",
};

/**
 * Compatibility wrapper over `Panel`.
 *
 * The icon is accepted but not drawn: small icon tiles in every header were one
 * of the "assembled admin template" tells. The title, hint and rule carry the
 * hierarchy instead.
 */
export function Section({
  children,
  title,
  hint,
  actions,
  tone = "default",
  className,
  bodyClassName,
  flush,
}: SectionProps) {
  return (
    <Panel
      title={title}
      hint={hint}
      actions={actions}
      tone={TONE[tone]}
      className={className}
      bodyClassName={bodyClassName}
      flush={flush}
    >
      {children}
    </Panel>
  );
}

export function SectionHeader({
  title,
  hint,
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
    <div className={cn("flex min-w-0 flex-wrap items-end justify-between gap-2", className)}>
      <div className="min-w-0">
        <h2 className="min-w-0 text-sm font-medium tracking-tight text-foreground">{title}</h2>
        {hint ? <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">{hint}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-1.5">{actions}</div> : null}
    </div>
  );
}
