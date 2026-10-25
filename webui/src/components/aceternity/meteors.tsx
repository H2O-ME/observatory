import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

interface MeteorsProps {
  number?: number;
  className?: string;
}

/**
 * Aceternity Meteors：流星雨背景层。
 *
 * Chromium 81 安全：纯 CSS 线性渐变 + transform 动画；prefers-reduced-motion
 * 时整体隐藏（在 index.css 的 meteor 规则里）。
 */
export const Meteors = ({ number = 12, className }: MeteorsProps) => {
  const [meteors, setMeteors] = useState<React.CSSProperties[]>([]);

  useEffect(() => {
    const styles: React.CSSProperties[] = [];
    for (let i = 0; i < number; i++) {
      styles.push({
        left: `${Math.floor(Math.random() * 100 - 20)}%`,
        top: `${Math.floor(Math.random() * 30 - 10)}%`,
        animationDelay: `${(Math.random() * 4).toFixed(2)}s`,
        animationDuration: `${Math.floor(Math.random() * 8 + 4)}s`,
      });
    }
    setMeteors(styles);
  }, [number]);

  return (
    <>
      {meteors.map((style, index) => (
        <span
          key={index}
          style={style}
          aria-hidden="true"
          className={cn("meteor pointer-events-none absolute h-0.5 w-0.5 rounded-full bg-ink", className)}
        />
      ))}
    </>
  );
};
