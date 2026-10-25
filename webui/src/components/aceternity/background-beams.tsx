/**
 * Aceternity UI · Background Beams — adapted build.
 * Upstream: https://ui.aceternity.com/registry/background-beams.json (see ./registry/).
 * Local changes: Chromium 81 syntax floor (no inset/translate/aspect
 * utilities, no color-mix), CJK tokenising, flat tokens, watch fallbacks.
 */

import { useId } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

/**
 * Curated beam paths, in a 1200×800 user-space box. Hand-written rather than
 * random so every render (and every screenshot) is identical.
 *
 * `tier` drives how much the small screens keep:
 *   0 — always drawn
 *   1 — hidden ≤900px (phones) via `.ace-beams__path--opt`
 *   2 — hidden on the watch via `.ace-beams__path--lite`
 */
const BEAMS: ReadonlyArray<{ d: string; tier: 0 | 1 | 2 }> = [
  { d: "M-160 860 C 60 750 120 620 300 520 C 470 424 560 372 720 268 C 880 164 980 92 1180 -40", tier: 0 },
  { d: "M-80 900 C 140 790 200 660 380 560 C 550 464 640 412 800 308 C 960 204 1060 132 1260 0", tier: 0 },
  { d: "M-220 820 C 0 710 60 580 240 480 C 410 384 500 332 660 228 C 820 124 920 52 1120 -80", tier: 0 },
  { d: "M-40 940 C 180 830 240 700 420 600 C 590 504 680 452 840 348 C 1000 244 1100 172 1300 40", tier: 0 },
  { d: "M-300 780 C -80 670 -20 540 160 440 C 330 344 420 292 580 188 C 740 84 840 12 1040 -120", tier: 0 },
  { d: "M-120 1000 C 100 890 160 760 340 660 C 510 564 600 512 760 408 C 920 304 1020 232 1220 100", tier: 0 },
  { d: "M-200 880 C 20 770 80 640 260 540 C 430 444 520 392 680 288 C 840 184 940 112 1140 -20", tier: 1 },
  { d: "M0 820 C 220 710 280 580 460 480 C 630 384 720 332 880 228 C 1040 124 1140 52 1340 -80", tier: 1 },
  { d: "M-260 940 C -40 830 20 700 200 600 C 370 504 460 452 620 348 C 780 244 880 172 1080 40", tier: 1 },
  { d: "M-60 860 C 160 750 220 620 400 520 C 570 424 660 372 820 268 C 980 164 1080 92 1280 -40", tier: 1 },
  { d: "M-180 920 C 40 810 100 680 280 580 C 450 484 540 432 700 328 C 860 224 960 152 1160 20", tier: 2 },
  { d: "M-320 860 C -100 750 -40 620 140 520 C 310 424 400 372 560 268 C 720 164 820 92 1020 -40", tier: 2 },
];

const DURATIONS = [9, 11, 13, 10, 14, 12, 15, 8, 16, 13, 11, 17] as const;
const DELAYS = [0, -1.6, -3.1, -4.7, -2.3, -6.2, -7.4, -1.1, -8.8, -5.3, -10.1, -11.7] as const;

export interface BackgroundBeamsProps {
  className?: string;
  /** How many curated beams to draw (1–12). Fewer on constrained screens. */
  count?: number;
  /** Pin to the viewport instead of the nearest positioned ancestor. */
  fixed?: boolean;
  /** Beam stroke width in SVG user units. */
  strokeWidth?: number;
}

/**
 * Aceternity-style travelling beams, rebuilt for Chromium 81.
 *
 * Every beam is an SVG path with `pathLength="100"`, `stroke-dasharray: 3 97`
 * and a CSS `stroke-dashoffset` animation — no `getPointAtLength`, no rAF, no
 * external dependency. The gradient stroke and the fade mask are static.
 *
 * `preserveAspectRatio="xMidYMid slice"` keeps the fan full-bleed without ever
 * creating horizontal overflow.
 */
export function BackgroundBeams({
  className,
  count = 12,
  fixed = false,
  strokeWidth = 1.15,
}: BackgroundBeamsProps) {
  const rawId = useId();
  const gradientId = `ace-beams-${rawId.replace(/[^a-zA-Z0-9_-]/g, "")}`;
  const beams = BEAMS.slice(0, Math.max(1, Math.min(count, BEAMS.length)));

  return (
    <div
      className={cn("ace-beams", fixed && "ace-beams--fixed", className)}
      aria-hidden="true"
    >
      <svg
        className="ace-beams__svg"
        viewBox="0 0 1200 800"
        preserveAspectRatio="xMidYMid slice"
        xmlns="http://www.w3.org/2000/svg"
        aria-hidden="true"
        focusable="false"
      >
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" style={{ stopColor: "var(--c-brand-3)", stopOpacity: 0 }} />
            <stop offset="26%" style={{ stopColor: "var(--c-brand-3)", stopOpacity: 0.9 }} />
            <stop offset="58%" style={{ stopColor: "var(--c-brand)", stopOpacity: 0.75 }} />
            <stop offset="82%" style={{ stopColor: "var(--c-brand-2)", stopOpacity: 0.55 }} />
            <stop offset="100%" style={{ stopColor: "var(--c-brand-2)", stopOpacity: 0 }} />
          </linearGradient>
        </defs>
        <g stroke={`url(#${gradientId})`}>
          {beams.map((beam, index) => (
            <path
              key={beam.d}
              d={beam.d}
              pathLength={100}
              strokeWidth={strokeWidth}
              className={cn(
                "ace-beam",
                beam.tier === 1 && "ace-beams__path--opt",
                beam.tier === 2 && "ace-beams__path--lite",
              )}
              style={{
                animationDuration: `${DURATIONS[index] ?? 12}s`,
                animationDelay: `${DELAYS[index] ?? 0}s`,
              }}
            />
          ))}
        </g>
      </svg>
    </div>
  );
}
