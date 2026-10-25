import { useId } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export interface GridPatternProps {
  width?: number;
  height?: number;
  x?: number;
  y?: number;
  strokeDasharray?: string;
  className?: string;
  /** Line/dot colour; defaults to the theme divider. */
  stroke?: string;
  /** Dots at the centre of each cell instead of a line grid. */
  dots?: boolean;
  /** Dot radius when `dots` is set. */
  dotSize?: number;
  /** Built-in Chromium 81-safe fade mask (use instead of a `[mask-image:…]` utility). */
  fade?: "none" | "radial" | "top";
}

/**
 * SVG grid/dot field for the login page, empty states and hero panels.
 *
 * The `<pattern>` id is derived from `useId()` and sanitised, so several
 * patterns can coexist on one page without one shadowing the others.
 *
 * Use `fade="radial"` rather than the Tailwind `[mask-image:…]` arbitrary
 * property: the latter emits unprefixed `mask-image`, which Chromium 81 drops.
 */
export function GridPattern({
  width = 40,
  height = 40,
  x = -1,
  y = -1,
  strokeDasharray = "none",
  className,
  stroke = "var(--c-line-strong)",
  dots = false,
  dotSize = 1.5,
  fade = "none",
}: GridPatternProps) {
  const rawId = useId();
  const patternId = `ace-grid-${rawId.replace(/[^a-zA-Z0-9_-]/g, "")}`;

  return (
    <svg
      aria-hidden="true"
      focusable="false"
      className={cn(
        "pointer-events-none absolute top-0 right-0 bottom-0 left-0 h-full w-full",
        fade === "radial" && "ace-grid--fade-radial",
        fade === "top" && "ace-grid--fade-top",
        className,
      )}
    >
      <defs>
        <pattern
          id={patternId}
          width={width}
          height={height}
          patternUnits="userSpaceOnUse"
          x={x}
          y={y}
        >
          {dots ? (
            <circle cx={width / 2} cy={height / 2} r={dotSize} fill={stroke} />
          ) : (
            <path
              d={`M ${height} 0 L 0 0 0 ${width}`}
              fill="none"
              stroke={stroke}
              strokeWidth={1}
              strokeDasharray={strokeDasharray}
            />
          )}
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill={`url(#${patternId})`} />
    </svg>
  );
}
