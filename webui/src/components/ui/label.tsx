import * as React from "react"
import { cn } from "@/lib/utils"

// Radix Themes 迁移（0.3.8）注：Text as="label" 不接受原生 label 属性
// （htmlFor/form），表单契约要求保留原生元素。
function Label({ className, ...props }: React.ComponentProps<"label">) {
  return (
    <label
      data-slot="label"
      className={cn(
        "flex items-center gap-2 text-[0.8rem] leading-none font-medium text-soft select-none",
        className
      )}
      {...props}
    />
  )
}

export { Label }
