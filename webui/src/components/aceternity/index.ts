/**
 * Aceternity kit · observatory edition
 *
 * Barrel for the visual-identity components. Importing this file also pulls in
 * `aceternity.css`; every component imports it on its own as well, so deep
 * imports (`@/components/aceternity/glow-card`) are equally safe.
 *
 * Chromium 81 notes and the watch fallbacks live in `aceternity.css`.
 */

import "./aceternity.css";

/* Atmosphere / background layers */
export { AuroraBackground } from "./aurora-background";
export type { AuroraBackgroundProps } from "./aurora-background";

export { BackgroundBeams } from "./background-beams";
export type { BackgroundBeamsProps } from "./background-beams";

export { LampEffect } from "./lamp-effect";
export type { LampEffectProps } from "./lamp-effect";

export { Sparkles } from "./sparkles";
export type { SparklesProps } from "./sparkles";

export { Meteors } from "./meteors";
export type { MeteorsProps } from "./meteors";

export { GridPattern } from "./grid-pattern";
export type { GridPatternProps } from "./grid-pattern";

export { Spotlight } from "./spotlight";
export type { SpotlightProps } from "./spotlight";

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

export { InfiniteMovingCards } from "./infinite-moving-cards";
export type { InfiniteMovingCardsProps } from "./infinite-moving-cards";

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
