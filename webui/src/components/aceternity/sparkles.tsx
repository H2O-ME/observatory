import type { CSSProperties } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export interface SparklesProps {
  className?: string;
  /** Particle count, clamped to 1–40. */
  count?: number;
  /** Sparkle colour; defaults to `var(--c-brand-3)`. */
  color?: string;
  /** Base particle size in px (each one varies between 0.6× and 1.4×). */
  size?: number;
}

/** Deterministic pseudo-random in [0, 1): identical on every render. */
function hash(index: number, salt: number): number {
  const value = Math.sin(index * 127.1 + salt * 311.7) * 43758.5453;
  return value - Math.floor(value);
}

/**
 * Lightweight SVG sparkle field for empty states and the login hero.
 *
 * Positions, sizes and twinkle timing come from a deterministic hash, so
 * screenshots are stable and re-renders never reshuffle the particles.
 * Chromium 81: static inline SVG + `transform`/`opacity` keyframes. Hidden on
 * the watch and frozen under reduced motion (see aceternity.css).
 */
export function Sparkles({ className, count = 14, color, size = 12 }: SparklesProps) {
  const total = Math.max(1, Math.min(Math.round(count), 40));

  const particles = Array.from({ length: total }, (_, index) => {
    const left = hash(index, 1) * 100;
    const top = hash(index, 2) * 100;
    const scale = 0.6 + hash(index, 3) * 0.8;
    const delay = -(hash(index, 4) * 7);
    const duration = 3.4 + hash(index, 5) * 3.2;
    const dimension = Math.max(4, size * scale);
    return {
      key: `sparkle-${index}`,
      left,
      top,
      delay,
      duration,
      dimension,
    };
  });

  return (
    <div className={cn("ace-sparkles", className)} aria-hidden="true">
      {particles.map((particle) => (
        <svg
          key={particle.key}
          className="ace-sparkle"
          viewBox="0 0 24 24"
          width={particle.dimension}
          height={particle.dimension}
          style={
            {
              left: `${particle.left}%`,
              top: `${particle.top}%`,
              width: `${particle.dimension}px`,
              height: `${particle.dimension}px`,
              color,
              animationDelay: `${particle.delay}s`,
              animationDuration: `${particle.duration}s`,
            } as CSSProperties
          }
          fill="none"
          aria-hidden="true"
          focusable="false"
        >
          <path
            d="M12 0 L14.3 9.7 L24 12 L14.3 14.3 L12 24 L9.7 14.3 L0 12 L9.7 9.7 Z"
            fill="currentColor"
          />
        </svg>
      ))}
    </div>
  );
}
