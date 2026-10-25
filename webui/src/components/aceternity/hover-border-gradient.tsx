import { useState } from "react";
import type { ReactNode } from "react";

import { motion } from "framer-motion";

import { cn } from "@/lib/utils";

interface HoverBorderGradientProps {
  children: ReactNode;
  containerClassName?: string;
  className?: string;
  duration?: number;
  clockwise?: boolean;
  onClick?: () => void;
  type?: "button" | "submit";
  disabled?: boolean;
}

/**
 * Aceternity HoverBorderGradient：悬停时边框泛起旋转渐变光的按钮。
 *
 * Chromium 81 安全：conic-gradient（Ch69+）+ CSS 旋转动画 + transform。
 * 与 shadcn Button 不同，它天生就是“描边发光”风格——用于登录主按钮和
 * 页面主操作。
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
}: HoverBorderGradientProps) {
  const [hovered, setHovered] = useState<boolean>(false);

  return (
    <button
      type={type}
      disabled={disabled}
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      className={cn(
        "relative flex min-h-11 min-w-44 items-center justify-center overflow-hidden rounded-xl border border-line bg-transparent p-[1.5px] text-sm",
        disabled && "cursor-not-allowed opacity-50",
        containerClassName,
      )}
    >
      <span
        aria-hidden="true"
        className={cn("border-spin-bg", hovered ? "opacity-100" : "opacity-0")}
        style={{
          background: "conic-gradient(from 0deg, #0a84ff, #6366f1, #38bdf8, #a78bfa, #0a84ff)",
          animationDuration: `${duration}s`,
          animationDirection: clockwise ? "normal" : "reverse",
        }}
      />
      <span
        className={cn(
          "relative z-10 flex w-full items-center justify-center rounded-[10px] bg-raised px-5 py-2.5 font-medium text-ink transition-colors",
          hovered && "text-brand",
          className,
        )}
      >
        {children}
      </span>
      {/* reduced-motion / 不悬停时兜底的细边 */}
      <span aria-hidden="true" className="pointer-events-none absolute inset-0 rounded-xl border border-line" />
    </button>
  );
}

/** 页面内联使用（不是按钮形态）时的包装变体。 */
export function BorderGradientBox({
  children,
  className,
  active = false,
}: {
  children: ReactNode;
  className?: string;
  active?: boolean;
}) {
  return (
    <motion.div
      whileHover={{ scale: 1.01 }}
      className={cn("relative overflow-hidden rounded-panel p-[1.5px]", className)}
    >
      <span
        aria-hidden="true"
        className={cn("border-spin-bg", active ? "opacity-100" : "opacity-0")}
        style={{ background: "conic-gradient(from 0deg, #0a84ff, #6366f1, #38bdf8, #0a84ff)" }}
      />
      <div className="relative z-10 h-full rounded-panel bg-panel">{children}</div>
      <span aria-hidden="true" className="pointer-events-none absolute inset-0 rounded-panel border border-line" />
    </motion.div>
  );
}
