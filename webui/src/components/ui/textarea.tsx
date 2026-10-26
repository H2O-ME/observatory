import * as React from "react"
import { cn } from "@/lib/utils"

// 同 input.tsx：保留原生元素（Radix Textarea 外层 div 会改变布局契约）。
function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        "flex min-h-16 w-full rounded-lg border border-line bg-sunken px-2.5 py-2 text-sm text-ink transition-colors outline-none placeholder:text-faint focus-visible:border-brand focus-visible:ring-2 focus-visible:ring-brand-soft disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-danger aria-invalid:ring-2 aria-invalid:ring-danger-soft",
        className
      )}
      {...props}
    />
  )
}

export { Textarea }
