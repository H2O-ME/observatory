import type { ReactNode } from "react";

import { useViewport } from "@/hooks/use-viewport";
import { cn } from "@/lib/utils";

interface PageShellProps {
  title: ReactNode;
  subtitle?: ReactNode;
  /** 右侧动作区（桌面同行、窄屏换行到标题下）。 */
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}

/**
 * 页面骨架（0.3.3）：统一的标题/动作/内容节奏与三端字号。
 *
 * 手表端标题 15px、动作区折行；手机 17px；桌面 20px——比 0.3.2 之前整体
 * 大一档，结束“全站 11px 小字”的时代。
 */
export function PageShell({ title, subtitle, actions, children, className }: PageShellProps) {
  const viewport = useViewport();
  const watch = viewport === "watch";
  return (
    <div className={cn("flex flex-col", watch ? "gap-2.5" : "gap-4", className)}>
      <div className={cn("flex flex-wrap items-center", watch ? "gap-2" : "gap-3")}>
        <h1
          className={cn(
            "min-w-0 font-semibold tracking-tight text-ink",
            watch ? "text-[0.95rem]" : viewport === "mobile" ? "text-[1.05rem]" : "text-xl",
          )}
        >
          {title}
        </h1>
        {actions ? (
          <div className={cn("flex items-center gap-2", watch ? "w-full" : "ml-auto")}>{actions}</div>
        ) : null}
      </div>
      {subtitle ? (
        <p className={cn("text-soft", watch ? "text-[0.7rem]" : "text-xs")}>{subtitle}</p>
      ) : null}
      {children}
    </div>
  );
}
