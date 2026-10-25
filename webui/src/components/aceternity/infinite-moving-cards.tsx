import type { CSSProperties, ReactNode } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export interface InfiniteMovingCardsProps {
  /** Card content. The list is duplicated internally for the seamless loop. */
  items: ReactNode[];
  className?: string;
  itemClassName?: string;
  direction?: "left" | "right";
  /** Seconds for one full loop; 30–50s reads as ambient. */
  duration?: number;
  /** Pause while hovered/focused (desktop only, costs nothing on touch). */
  pauseOnHover?: boolean;
  /** Accessible name; the duplicated copy is always `aria-hidden`. */
  label?: string;
}

/**
 * CSS-only marquee. The track holds two identical groups and animates
 * `translate3d(0) → translate3d(-50%)`, so the seam is invisible and there is
 * no JS frame loop.
 *
 * Chromium 81: `transform` animation only, no `gap` (items use margins).
 * With reduced motion, or on the watch, the animation stops and the strip
 * becomes a normal horizontal scroller instead of vanishing.
 */
export function InfiniteMovingCards({
  items,
  className,
  itemClassName,
  direction = "left",
  duration = 38,
  pauseOnHover = false,
  label,
}: InfiniteMovingCardsProps) {
  const style = {
    "--ace-marquee-dur": `${Math.max(8, duration)}s`,
  } as CSSProperties;

  return (
    <div
      className={cn(
        "ace-marquee",
        direction === "right" && "ace-marquee--right",
        pauseOnHover && "ace-marquee--pause",
        className,
      )}
      style={style}
      aria-label={label}
      role={label ? "group" : undefined}
    >
      <div className="ace-marquee__track">
        {[0, 1].map((copy) => (
          <div
            key={`copy-${copy}`}
            className="ace-marquee__group"
            aria-hidden={copy === 1 ? "true" : undefined}
          >
            {items.map((item, index) => (
              // Duplicated, static content: the index is a stable identity here.
              <div
                key={`card-${copy}-${index}`}
                className={cn("ace-marquee__item", itemClassName)}
              >
                {item}
              </div>
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}
