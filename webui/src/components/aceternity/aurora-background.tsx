import type { CSSProperties } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export interface AuroraBackgroundProps {
  /** Extra classes for the layer (position/opacity overrides are common). */
  className?: string;
  /** Pin to the viewport instead of the nearest positioned ancestor. */
  fixed?: boolean;
  /** Overall intensity multiplier, 0–1. */
  intensity?: number;
}

/**
 * Full-bleed aurora wash: two stacked repeating-gradient layers drifting on
 * different durations. Pure CSS (`background-position` keyframes) — no JS
 * animation, no rAF, no canvas.
 *
 * Chromium 81: `-webkit-mask-image` fallbacks are declared first; the watch
 * media query stops the drift and drops the second layer (see aceternity.css).
 */
export function AuroraBackground({
  className,
  fixed = false,
  intensity,
}: AuroraBackgroundProps) {
  const style =
    intensity == null
      ? undefined
      : ({ opacity: Math.max(0, Math.min(1, intensity)) } as CSSProperties);

  return (
    <div
      className={cn("ace-aurora", fixed && "ace-aurora--fixed", className)}
      style={style}
      aria-hidden="true"
    >
      <span className="ace-aurora__layer ace-aurora__layer--a" />
      <span className="ace-aurora__layer ace-aurora__layer--b" />
    </div>
  );
}
