/**
 * Aceternity UI · Moving Border — adapted build.
 * Upstream: https://ui.aceternity.com/registry/moving-border.json (see ./registry/).
 * Local changes: Chromium 81 syntax floor (no inset/translate/aspect
 * utilities, no color-mix), CJK tokenising, flat tokens, watch fallbacks.
 */

import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export interface MovingBorderProps {
  children: ReactNode;
  /** One full colour cycle in milliseconds (kept from the old API). */
  duration?: number;
  className?: string;
  containerClassName?: string;
  /**
   * Gradient stops. Defaults to the brand blue → violet → cyan scale.
   * CSS colour strings, var() tokens included.
   */
  colors?: string[];
  /** Flow the other way round. */
  reverse?: boolean;
}

const DEFAULT_COLORS = [
  "var(--c-brand)",
  "var(--c-brand-2)",
  "var(--c-brand-3)",
  "var(--c-brand)",
];

/**
 * Aceternity moving border — a flowing gradient ring around any block.
 *
 * Implementation note: instead of the old rotating oversized conic layer (which
 * covers a square fine but breaks on wide pills, and needs a viewport-sized
 * compositing layer), the gradient is painted only into the 1.5px frame with
 * the `-webkit-mask` xor trick and swept via `background-position`. That is
 * aspect-ratio agnostic, cheap, and Chromium 81 safe (`conic-gradient` is no
 * longer required at all).
 *
 * `duration` stays in milliseconds for backwards compatibility.
 */
export function MovingBorder({
  children,
  duration = 8000,
  className,
  containerClassName,
  colors = DEFAULT_COLORS,
  reverse = false,
}: MovingBorderProps) {
  const stops = [...colors, colors[0] ?? DEFAULT_COLORS[0]].join(", ");

  return (
    <div
      className={cn(
        "relative overflow-hidden rounded-xl p-[1.5px]",
        containerClassName,
      )}
    >
      <span
        aria-hidden="true"
        className={cn("ace-spin-border", reverse && "ace-spin-border--reverse")}
        style={{
          backgroundImage: `linear-gradient(115deg, ${stops})`,
          animationDuration: `${Math.max(1200, duration)}ms`,
        }}
      />
      <div className={cn("relative z-10 h-full w-full", className)}>
        {children}
      </div>
    </div>
  );
}
