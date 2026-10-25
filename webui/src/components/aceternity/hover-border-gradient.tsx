import { useState } from "react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

const RING_COLORS = [
  "var(--c-brand-3)",
  "var(--c-brand)",
  "var(--c-brand-2)",
  "var(--c-brand-3)",
];
const RING_GRADIENT = `linear-gradient(115deg, ${RING_COLORS.join(", ")})`;

export interface HoverBorderGradientProps {
  children: ReactNode;
  containerClassName?: string;
  className?: string;
  /** Seconds for one ring cycle (kept from the old API). */
  duration?: number;
  clockwise?: boolean;
  onClick?: () => void;
  type?: "button" | "submit";
  disabled?: boolean;
  /** Fill the parent width. Width is otherwise intrinsic — the old forced
   *  `min-w-44` made the button overflow a 320px watch viewport. */
  block?: boolean;
  title?: string;
  ariaLabel?: string;
}

/**
 * Primary CTA with a gradient ring that appears on hover/focus and flows around
 * the border.
 *
 * Fixed from the previous version: no `min-w-44` (width is intrinsic unless
 * `block`), no framer-motion, no dependency on a global `.border-spin-bg`
 * class, and the ring is only mounted while hovered/focused so idle buttons
 * cost nothing on the watch.
 */
export function HoverBorderGradient({
  children,
  containerClassName,
  className,
  duration = 1,
  clockwise = true,
  onClick,
  type = "button",
  disabled = false,
  block = false,
  title,
  ariaLabel,
}: HoverBorderGradientProps) {
  const [active, setActive] = useState(false);
  const showRing = active && !disabled;

  return (
    <button
      type={type}
      disabled={disabled}
      onClick={onClick}
      title={title}
      aria-label={ariaLabel}
      onMouseEnter={() => setActive(true)}
      onMouseLeave={() => setActive(false)}
      onFocus={() => setActive(true)}
      onBlur={() => setActive(false)}
      className={cn(
        "relative inline-flex max-w-full items-center justify-center overflow-hidden rounded-xl border border-line bg-transparent p-[1.5px] text-sm",
        block && "flex w-full",
        disabled && "cursor-not-allowed opacity-50",
        containerClassName,
      )}
    >
      {showRing ? (
        <span
          aria-hidden="true"
          className="ace-spin-border"
          style={{
            backgroundImage: RING_GRADIENT,
            animationDuration: `${Math.max(0.4, duration)}s`,
            animationDirection: clockwise ? "normal" : "reverse",
          }}
        />
      ) : null}
      <span
        className={cn(
          "relative z-10 flex w-full min-w-0 items-center justify-center rounded-[10px] bg-raised px-5 py-2.5 font-medium text-ink transition-colors",
          showRing && "text-brand",
          className,
        )}
      >
        {children}
      </span>
      {/* Static hairline so the button keeps a border when the ring is off. */}
      <span
        aria-hidden="true"
        className="pointer-events-none absolute top-0 right-0 bottom-0 left-0 rounded-xl border border-line"
      />
    </button>
  );
}

export interface BorderGradientBoxProps {
  children: ReactNode;
  className?: string;
  /** Keep the ring lit (selected/loading states). */
  active?: boolean;
}

/** Non-button variant: a panel whose border lights up while `active`. */
export function BorderGradientBox({
  children,
  className,
  active = false,
}: BorderGradientBoxProps) {
  return (
    <div
      className={cn(
        "relative overflow-hidden rounded-panel p-[1.5px]",
        className,
      )}
    >
      {active ? (
        <span
          aria-hidden="true"
          className="ace-spin-border"
          style={{ backgroundImage: RING_GRADIENT, animationDuration: "6s" }}
        />
      ) : null}
      <div className="relative z-10 h-full rounded-panel bg-panel">{children}</div>
      <span
        aria-hidden="true"
        className="pointer-events-none absolute top-0 right-0 bottom-0 left-0 rounded-panel border border-line"
      />
    </div>
  );
}
