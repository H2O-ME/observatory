import * as React from "react"
import { Input as InputPrimitive } from "@base-ui/react/input"
import { cn } from "@/lib/utils"

// Radix Themes 迁移（0.3.8）注：TextField.Root 会包一层布局 div，破坏
// InputGroup 的 flex 结构与裸 input 语义，因此输入框保留原生元素实现，
// 由 Theme 令牌（accent indigo / radius small）统一观感。
function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <InputPrimitive
      type={type}
      data-slot="input"
      className={cn(
        "h-10 w-full min-w-0 rounded-xl border border-line bg-sunken px-3 py-1.5 text-sm text-ink transition-colors outline-none placeholder:text-faint focus-visible:border-brand focus-visible:ring-2 focus-visible:ring-brand-soft disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-danger aria-invalid:ring-2 aria-invalid:ring-danger-soft",
        className
      )}
      {...props}
    />
  )
}

export { Input }
