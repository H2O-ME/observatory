import type { CSSProperties, ReactNode } from "react";

import { Link } from "react-router-dom";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export interface FloatingDockItem {
  /** Router path, e.g. `/wall`. */
  href: string;
  label: string;
  icon: ReactNode;
  active?: boolean;
  /** Count/dot badge in the corner of the icon. */
  badge?: string | number;
  /** When set, the item renders as a button instead of a router link. */
  onClick?: () => void;
}

export interface FloatingDockProps {
  /** 5–6 items is the sweet spot; the grid divides evenly by item count. */
  items: FloatingDockItem[];
  className?: string;
  /** Force the icon-only layout. The watch media query does this on its own. */
  compact?: boolean;
  /** Accessible name of the `<nav>` landmark. */
  label?: string;
}

/**
 * Bottom navigation for watch/mobile (desktop keeps the floating sidebar).
 *
 * `position: fixed` with `env(safe-area-inset-bottom)` padding, 44–52px tap
 * targets and a grid that gives every item an equal slice (never flex `gap` —
 * that collapses in Chromium 81). Labels collapse to icons below 480×560.
 *
 * Rendered with react-router's `Link`, so it must live inside the router.
 * Remember to reserve space: the shell should pad its scroll area by the dock
 * height so content is not hidden behind it.
 */
export function FloatingDock({
  items,
  className,
  compact = false,
  label = "主导航",
}: FloatingDockProps) {
  const columns = Math.max(items.length, 1);
  const style = { "--ace-dock-cols": String(columns) } as CSSProperties;

  const content = (item: FloatingDockItem) => (
    <>
      <span className="ace-dock__icon" aria-hidden="true">
        {item.icon}
      </span>
      <span className="ace-dock__label">{item.label}</span>
      {item.badge != null && item.badge !== "" ? (
        <span className="ace-dock__badge">{item.badge}</span>
      ) : null}
    </>
  );

  return (
    <nav
      className={cn("ace-dock", compact && "ace-dock--compact", className)}
      aria-label={label}
      style={style}
    >
      <ul className="ace-dock__list">
        {items.map((item) => (
          <li key={item.href} className="ace-dock__item">
            {item.onClick ? (
              <button
                type="button"
                className={cn("ace-dock__link", item.active && "is-active")}
                aria-label={item.label}
                title={item.label}
                onClick={item.onClick}
              >
                {content(item)}
              </button>
            ) : (
              <Link
                to={item.href}
                className={cn("ace-dock__link", item.active && "is-active")}
                aria-current={item.active ? "page" : undefined}
                aria-label={item.label}
                title={item.label}
              >
                {content(item)}
              </Link>
            )}
          </li>
        ))}
      </ul>
    </nav>
  );
}
