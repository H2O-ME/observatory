import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

interface MovingBorderProps {
  children: ReactNode;
  /** 一圈流转的毫秒数；默认 8s（导航态足够从容）。 */
  duration?: number;
  className?: string;
  containerClassName?: string;
  /** 描边渐变角度起点色序列；默认品牌蓝紫。 */
  colors?: string[];
}

/**
 * Aceternity Moving Border：沿边框持续流转的渐变描边（导航当前项、当前
 * Tab、激活卡片）。实现为超大旋转锥形渐变被内层实底裁出细边——纯 CSS
 * （conic-gradient Ch69+ / transform 动画），Chromium 81 安全，reduced
 * -motion 下退化为静态渐变边框（见 .border-spin-bg）。
 */
export function MovingBorder({
  children,
  duration = 8000,
  className,
  containerClassName,
  colors = ["#0a84ff", "#6366f1", "#38bdf8", "#0a84ff"],
}: MovingBorderProps) {
  return (
    <div className={cn("relative overflow-hidden p-[1.5px]", containerClassName)}>
      <span
        aria-hidden="true"
        className="border-spin-bg"
        style={{
          background: `conic-gradient(from 0deg, ${colors.join(", ")})`,
          animationDuration: `${duration}ms`,
        }}
      />
      <div className={cn("relative z-10 h-full w-full", className)}>{children}</div>
    </div>
  );
}
