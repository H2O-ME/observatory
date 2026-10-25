/**
 * Aceternity UI · Animated Tooltip — adapted build.
 * Upstream: https://ui.aceternity.com/registry/animated-tooltip.json (see ./registry/).
 * Local changes: Chromium 81 syntax floor (no inset/translate/aspect
 * utilities, no color-mix), CJK tokenising, flat tokens, watch fallbacks.
 */

import { useId } from "react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export type TooltipSide = "top" | "bottom" | "end";

export interface AnimatedTooltipProps {
  /** Bubble content — keep it short (one line, nowrap). */
  content: ReactNode;
  /** The trigger; it must be focusable for keyboard users to see the bubble. */
  children: ReactNode;
  className?: string;
  side?: TooltipSide;
  /** Force the bubble open (first-run hint, tests). */
  open?: boolean;
}

/**
 * Desktop tooltip with no portal and no positioning library: the bubble is a
 * sibling of the trigger, revealed by `:hover` and `:focus-within` (so it also
 * appears for keyboard focus), and hidden entirely on coarse pointers where
 * hover does not exist.
 *
 * The trigger should still carry its own `aria-label`/`title` — the bubble is
 * a visual affordance, `role="tooltip"` is only announced by some readers.
 */
export function AnimatedTooltip({
  content,
  children,
  className,
  side = "top",
  open = false,
}: AnimatedTooltipProps) {
  const rawId = useId();
  const tipId = `ace-tip-${rawId.replace(/[^a-zA-Z0-9_-]/g, "")}`;

  return (
    <span
      className={cn(
        "ace-tip",
        side === "bottom" && "ace-tip--bottom",
        side === "end" && "ace-tip--end",
        open && "ace-tip--open",
        className,
      )}
    >
      {children}
      <span role="tooltip" id={tipId} className="ace-tip__bubble">
        {content}
      </span>
    </span>
  );
}
