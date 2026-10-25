import type { CSSProperties, ReactNode } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export interface LampEffectProps {
  className?: string;
  /** Content that sits under the lamp (typically the login heading + card). */
  children?: ReactNode;
  contentClassName?: string;
  /**
   * Override the cone tint — any CSS colour, e.g. `rgba(255, 214, 102, 0.35)`.
   * Defaults to the brand blue scale in both themes.
   */
  color?: string;
}

/**
 * Aceternity lamp: a conic light cone falling from a bright hairline at the
 * top edge, with a soft radial bloom behind it.
 *
 * Chromium 81: `conic-gradient` (Ch69), `min()` (Ch79), animated `transform` +
 * `opacity` only. The entry animation never blocks layout, and reduced motion
 * leaves the lamp lit but static.
 */
export function LampEffect({
  className,
  children,
  contentClassName,
  color,
}: LampEffectProps) {
  const style = color
    ? ({
        "--ace-lamp-core": color,
        "--ace-lamp-line": color,
      } as CSSProperties)
    : undefined;

  return (
    <div className={cn("ace-lamp", className)} style={style}>
      <span className="ace-lamp__glow" aria-hidden="true" />
      <span className="ace-lamp__cone" aria-hidden="true" />
      <span className="ace-lamp__line" aria-hidden="true" />
      {children ? (
        <div className={cn("ace-lamp__content", contentClassName)}>{children}</div>
      ) : null}
    </div>
  );
}
