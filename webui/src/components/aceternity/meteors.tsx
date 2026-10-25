import { useMemo } from "react";
import type { CSSProperties } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export interface MeteorsProps {
  /** Number of meteors, clamped to 1–40. */
  number?: number;
  className?: string;
  /** Base fall duration in seconds; each meteor varies around it. */
  duration?: number;
  /** Fixed seed so the field is stable across renders. */
  seed?: number;
}

/** Deterministic pseudo-random in [0, 1). */
function hash(index: number, salt: number, seed: number): number {
  const value = Math.sin((index + seed) * 127.1 + salt * 311.7) * 43758.5453;
  return value - Math.floor(value);
}

/**
 * Aceternity meteors, rebuilt for the watch.
 *
 * Positions/timing are generated from a seeded hash instead of `Math.random()`,
 * so a re-render (or a React StrictMode double render) never re-scatters the
 * field. The whole layer is hidden on the watch and under reduced motion in
 * `aceternity.css` — the GPU cost is not worth it there.
 */
export function Meteors({ number = 12, className, duration = 6, seed = 7 }: MeteorsProps) {
  const meteors = useMemo(() => {
    const total = Math.max(1, Math.min(Math.round(number), 40));
    return Array.from({ length: total }, (_, index) => {
      const left = hash(index, 2, seed) * 120 - 20;
      const top = hash(index, 3, seed) * 40 - 10;
      const delay = -(hash(index, 5, seed) * 8);
      const fall = duration * (0.7 + hash(index, 7, seed) * 0.9);
      return {
        key: `meteor-${seed}-${index}`,
        style: {
          left: `${left}%`,
          top: `${top}%`,
          animationDelay: `${delay.toFixed(2)}s`,
          animationDuration: `${fall.toFixed(2)}s`,
        } as CSSProperties,
      };
    });
  }, [number, duration, seed]);

  return (
    <div className={cn("ace-meteors", className)} aria-hidden="true">
      {meteors.map((meteor) => (
        <span key={meteor.key} className="ace-meteor" style={meteor.style} />
      ))}
    </div>
  );
}
