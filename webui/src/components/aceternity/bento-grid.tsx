import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export interface BentoGridProps {
  className?: string;
  children?: ReactNode;
}

/**
 * Responsive bento grid: 1 column on the watch, 2 on phones/small tablets,
 * 3 from 1100px up. Grid `gap` (CSS Grid, Chromium 57+) instead of flex `gap`,
 * so the watch keeps its spacing.
 */
export function BentoGrid({ className, children }: BentoGridProps) {
  return <div className={cn("ace-bento", className)}>{children}</div>;
}

export interface BentoGridItemProps {
  className?: string;
  title?: ReactNode;
  description?: ReactNode;
  /** Media strip on top of the tile (illustration, chart, gradient). */
  header?: ReactNode;
  icon?: ReactNode;
  /** Pinned to the bottom of the tile. */
  footer?: ReactNode;
  children?: ReactNode;
}

/**
 * Aceternity `BentoGridItem` with the same prop names (`title`, `description`,
 * `header`, `icon`, `className`) plus `footer` and free `children`.
 *
 * The tile can safely contain interactive content: it is an `<article>`, never
 * a button, and the hover lift is CSS-only.
 */
export function BentoGridItem({
  className,
  title,
  description,
  header,
  icon,
  footer,
  children,
}: BentoGridItemProps) {
  return (
    <article className={cn("ace-bento__item", className)}>
      {header ? <div className="ace-bento__header">{header}</div> : null}
      <div className="ace-bento__body">
        {icon ? (
          <div className="ace-bento__icon" aria-hidden="true">
            {icon}
          </div>
        ) : null}
        {title ? <h3 className="ace-bento__title">{title}</h3> : null}
        {description ? <p className="ace-bento__desc">{description}</p> : null}
        {children}
        {footer ? <div className="ace-bento__footer">{footer}</div> : null}
      </div>
    </article>
  );
}
