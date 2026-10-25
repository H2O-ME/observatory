import { useId } from "react";

import { cn } from "@/lib/utils";

interface GridPatternProps {
  width?: number;
  height?: number;
  x?: number;
  y?: number;
  strokeDasharray?: string;
  className?: string;
  /** 网格线颜色，默认跟随主题分隔线。 */
  stroke?: string;
}

/**
 * Aceternity Grid Pattern：SVG 网格背景。桌面端登录页 / 空态背景用。
 * 纯 SVG，无兼容性风险；动画版本（流动偏移）只在非 reduced-motion 时叠加。
 */
export function GridPattern({
  width = 40,
  height = 40,
  x = -1,
  y = -1,
  strokeDasharray = "none",
  className,
  stroke = "var(--c-line-strong)",
}: GridPatternProps) {
  const id = useId();
  return (
    <svg aria-hidden="true" className={cn("pointer-events-none absolute inset-0 h-full w-full", className)}>
      <defs>
        <pattern id={id} width={width} height={height} patternUnits="userSpaceOnUse" x={x} y={y}>
          <path
            d={`M ${height} 0 L 0 0 0 ${width}`}
            fill="none"
            stroke={stroke}
            strokeWidth="1"
            strokeDasharray={strokeDasharray}
          />
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill={`url(#${id})`} />
    </svg>
  );
}
