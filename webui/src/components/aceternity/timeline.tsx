import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export interface TimelineProps {
  className?: string;
  children?: ReactNode;
}

/**
 * Vertical timeline shell. Renders an `<ol>`; put `TimelineItem`s in it.
 * The connector line is drawn by the item markers in CSS (grid columns, so the
 * rail follows the writing direction instead of a hardcoded `left`).
 */
export function Timeline({ className, children }: TimelineProps) {
  return <ol className={cn("ace-timeline", className)}>{children}</ol>;
}

export interface TimelineItemProps {
  className?: string;
  /** Small caption above the title (usually a date/time). */
  date?: ReactNode;
  title?: ReactNode;
  description?: ReactNode;
  /** Extra body under the description (chips, buttons, a payload preview). */
  children?: ReactNode;
  /** Replaces the dot; the marker grows to fit. */
  icon?: ReactNode;
  /** Muted dot + line for historic/background entries. */
  quiet?: boolean;
}

/**
 * One timeline entry: marker column + body column. `date` / `title` /
 * `description` map to `.eyebrow`-sized caption, a 14px heading and soft body
 * copy; `children` is where per-page detail lives.
 */
export function TimelineItem({
  className,
  date,
  title,
  description,
  children,
  icon,
  quiet = false,
}: TimelineItemProps) {
  return (
    <li
      className={cn(
        "ace-timeline__item",
        quiet && "ace-timeline__item--quiet",
        icon && "ace-timeline__item--icon",
        className,
      )}
    >
      <div className="ace-timeline__marker" aria-hidden="true">
        <span className="ace-timeline__dot">
          {icon ? <span className="ace-timeline__glyph">{icon}</span> : null}
        </span>
      </div>
      <div className="ace-timeline__body">
        {date ? <span className="ace-timeline__date">{date}</span> : null}
        {title ? <h3 className="ace-timeline__title">{title}</h3> : null}
        {description ? <p className="ace-timeline__desc">{description}</p> : null}
        {children ? <div className="ace-timeline__extra">{children}</div> : null}
      </div>
    </li>
  );
}
