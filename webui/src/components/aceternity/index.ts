/**
 * Aceternity kit · observatory edition
 *
 * Standard source: the official registry, installed with the shadcn CLI —
 *   pnpm dlx shadcn@latest add @aceternity/<name>
 * (namespace configured in components.json → "registries"). The untouched
 * upstream files live in ./registry/ for diffing/upgrading; they are excluded
 * from the build (tsconfig.app.json). The components exported here are the
 * same registry items adapted for this console (each file's header lists its
 * changes): Chromium 81 syntax floor, CJK text, flat tokens, watch fallbacks.
 *
 * Importing this barrel also pulls in `aceternity.css`; every component
 * imports it on its own as well, so deep imports are equally safe.
 *
 * Chromium 81 notes and the watch fallbacks live in `aceternity.css`.
 */

import "./aceternity.css";

/* Atmosphere / background layers */
export { BackgroundBeams } from "./background-beams";
export type { BackgroundBeamsProps } from "./background-beams";

export { Sparkles } from "./sparkles";
export type { SparklesProps } from "./sparkles";

export { GridPattern } from "./grid-pattern";
export type { GridPatternProps } from "./grid-pattern";

/* Surfaces & layout */
export { BentoGrid, BentoGridItem } from "./bento-grid";
export type { BentoGridProps, BentoGridItemProps } from "./bento-grid";

export { GlowCard } from "./glow-card";
export type { GlowCardProps } from "./glow-card";

export { Timeline, TimelineItem } from "./timeline";
export type { TimelineProps, TimelineItemProps } from "./timeline";

export { TracingBeam } from "./tracing-beam";
export type { TracingBeamProps } from "./tracing-beam";

/* Interaction */
export { FloatingDock } from "./floating-dock";
export type { FloatingDockItem, FloatingDockProps } from "./floating-dock";

export { AnimatedTooltip } from "./animated-tooltip";
export type { AnimatedTooltipProps, TooltipSide } from "./animated-tooltip";

export { ShimmerButton } from "./shimmer-button";
export type { ShimmerButtonProps } from "./shimmer-button";

export { MovingBorder } from "./moving-border";
export type { MovingBorderProps } from "./moving-border";

export { BorderGradientBox, HoverBorderGradient } from "./hover-border-gradient";
export type {
  BorderGradientBoxProps,
  HoverBorderGradientProps,
} from "./hover-border-gradient";

export { TextGenerateEffect } from "./text-generate-effect";
export type {
  TextGenerateEffectProps,
  TextGenerateTag,
} from "./text-generate-effect";
