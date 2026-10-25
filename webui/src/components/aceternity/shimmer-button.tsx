/**
 * Aceternity UI · Shimmer Button — local implementation of the Pro (paid)
 * registry component (registry returns 401 without a licence), rebuilt
 * with the same visual contract but Chromium 81 safe and flat.
 */

import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export interface ShimmerButtonProps {
  children: ReactNode;
  className?: string;
  /** Native button type. Ignored when `href` is set. */
  type?: "button" | "submit" | "reset";
  disabled?: boolean;
  /** Shows the inline spinner, sets `aria-busy`, and blocks clicks. */
  busy?: boolean;
  /** Stretch to the container width (forms, sheets). */
  block?: boolean;
  /** When present the component renders an `<a>` instead of a `<button>`. */
  href?: string;
  title?: string;
  onClick?: () => void;
}

/**
 * Primary action button: brand gradient, a sheen that sweeps across on a loop
 * (accelerated on hover), and a busy state.
 *
 * Chromium 81: gradient background + `transform` keyframes. The sheen stops
 * under reduced motion, and the button stays a legible brand block.
 */
export function ShimmerButton({
  children,
  className,
  type = "button",
  disabled = false,
  busy = false,
  block = false,
  href,
  title,
  onClick,
}: ShimmerButtonProps) {
  const classes = cn(
    "ace-shimmer",
    block && "ace-shimmer--block",
    busy && "ace-shimmer--busy",
    className,
  );

  const content = (
    <>
      {!busy ? <span className="ace-shimmer__sheen" aria-hidden="true" /> : null}
      <span className="ace-shimmer__label">
        {busy ? <span className="ace-shimmer__spinner" aria-hidden="true" /> : null}
        {children}
      </span>
    </>
  );

  if (href) {
    return (
      <a
        href={href}
        className={classes}
        title={title}
        aria-busy={busy || undefined}
        aria-disabled={disabled || busy ? "true" : undefined}
        onClick={(event) => {
          if (disabled || busy) {
            event.preventDefault();
            return;
          }
          onClick?.();
        }}
      >
        {content}
      </a>
    );
  }

  return (
    <button
      type={type}
      className={classes}
      title={title}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      onClick={onClick}
    >
      {content}
    </button>
  );
}
