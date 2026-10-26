"use client"

import { Switch as RadixSwitch } from "@radix-ui/themes"
import { cn } from "@/lib/utils"

/**
 * Radix Themes 迁移（0.3.8）：开关本体交给 @radix-ui/themes（自带轨道/滑块/
 * 状态动画），size 保持旧 API。旧 `.ui-switch` 自定义样式停用；44px 命中区
 * 由伪元素补足（Chromium 81 无独立 translate 属性，勿改回 Tailwind translate）。
 */
function Switch({
  className,
  size = "default",
  ...props
}: React.ComponentProps<typeof RadixSwitch> & {
  size?: "sm" | "default"
}) {
  return (
    <RadixSwitch
      data-slot="switch"
      variant="soft"
      color="indigo"
      size={size === "sm" ? "1" : "2"}
      className={cn(
        "relative shrink-0",
        // 命中区扩到 44px，视觉轨道保持紧凑
        "after:absolute after:inset-[-10px] after:content-['']",
        className
      )}
      {...props}
    />
  )
}

export { Switch }
