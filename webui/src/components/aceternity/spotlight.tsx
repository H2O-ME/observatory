import { useId } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export interface SpotlightProps {
  className?: string;
  /** Beam colour; defaults to the brand token. */
  fill?: string;
}

/**
 * Aceternity spotlight — a single soft beam of light.
 *
 * Every instance mints its own SVG filter id from `useId()` (sanitised, because
 * React's raw id contains `:` and ends up inside `url(#…)`). The previous
 * implementation hardcoded `id="filter"`, so two spotlights on one page made
 * the browser resolve both `filter` references to the first filter — the
 * second beam silently disappeared.
 *
 * Chromium 81 safe: SVG `feGaussianBlur`, no CSS filters in the critical path.
 */
export function Spotlight({ className, fill = "var(--c-brand)" }: SpotlightProps) {
  const rawId = useId();
  const filterId = `ace-spotlight-${rawId.replace(/[^a-zA-Z0-9_-]/g, "")}`;

  return (
    <svg
      className={cn(
        "pointer-events-none absolute z-[1] h-[169%] w-[138%] opacity-40 lg:w-[84%]",
        className,
      )}
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 3787 2842"
      fill="none"
      aria-hidden="true"
      focusable="false"
    >
      <g filter={`url(#${filterId})`}>
        <ellipse
          cx="1924.71"
          cy="273.501"
          rx="718"
          ry="282"
          transform="matrix(-0.822377 -0.568943 -0.568943 0.822377 3631.88 2291.23)"
          fill={fill}
          fillOpacity="0.28"
        />
      </g>
      <defs>
        <filter
          id={filterId}
          x="0.860352"
          y="0.838989"
          width="3785.16"
          height="2840.26"
          filterUnits="userSpaceOnUse"
          colorInterpolationFilters="sRGB"
        >
          <feFlood floodOpacity="0" result="BackgroundImageFix" />
          <feBlend
            mode="normal"
            in="SourceGraphic"
            in2="BackgroundImageFix"
            result="shape"
          />
          <feGaussianBlur stdDeviation="151" result="effect1_foregroundBlur" />
        </filter>
      </defs>
    </svg>
  );
}
