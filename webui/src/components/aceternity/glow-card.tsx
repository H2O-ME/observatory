import type {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  ReactNode,
} from "react";

import { cn } from "@/lib/utils";

import "./aceternity.css";

export interface GlowCardProps {
  children?: ReactNode;
  className?: string;
  /** Padding and layout for the inner content. Defaults to `p-3 sm:p-4`. */
  contentClassName?: string;
  /**
   * Skip pointer tracking and always render the static top glow. Touch devices
   * and reduced-motion users get that variant automatically via CSS.
   */
  isStatic?: boolean;
  /** Spotlight tint — any CSS colour. Defaults to brand blue. */
  tint?: string;
  style?: CSSProperties;
  /** Makes the whole card clickable (and keyboard operable). */
  onClick?: () => void;
  /** Accessible name when the card is clickable and its children are not a label. */
  ariaLabel?: string;
}

/**
 * Panel with a pointer-following radial spotlight and a gradient hairline that
 * fades in on hover/focus.
 *
 * The spotlight position is written straight to the DOM as the `--ace-mx` /
 * `--ace-my` custom properties inside the pointer handler: no React state, so
 * moving the mouse never re-renders the card (and the tree underneath).
 *
 * Chromium 81: `getBoundingClientRect`, PointerEvents (Ch55) and the
 * `-webkit-mask` xor ring are all supported. Reduced motion and coarse
 * pointers fall back to a static top light; the ring still appears on focus.
 */
export function GlowCard({
  children,
  className,
  contentClassName,
  isStatic = false,
  tint,
  style,
  onClick,
  ariaLabel,
}: GlowCardProps) {
  const trackPointer = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (isStatic || event.pointerType === "touch") return;
    const element = event.currentTarget;
    const rect = element.getBoundingClientRect();
    element.style.setProperty("--ace-mx", `${event.clientX - rect.left}px`);
    element.style.setProperty("--ace-my", `${event.clientY - rect.top}px`);
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!onClick) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onClick();
    }
  };

  const cssStyle = {
    ...(tint ? { "--ace-glow-tint": tint } : null),
    ...style,
  } as CSSProperties;

  return (
    <div
      className={cn(
        "ace-glow",
        isStatic && "ace-glow--static",
        onClick && "cursor-pointer",
        className,
      )}
      style={cssStyle}
      onPointerMove={trackPointer}
      onClick={onClick}
      onKeyDown={onClick ? onKeyDown : undefined}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      aria-label={onClick ? ariaLabel : undefined}
    >
      <span className="ace-glow__spot" aria-hidden="true" />
      <div className={cn("ace-glow__content p-3 sm:p-4", contentClassName)}>
        {children}
      </div>
    </div>
  );
}
