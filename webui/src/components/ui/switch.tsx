"use client"

import { Switch as SwitchPrimitive } from "@base-ui/react/switch"
import { cn } from "@/lib/utils"

/**
 * Switch with explicit `transform` CSS rather than Tailwind `translate-*`
 * utilities (Chromium 81 on the watch lacks the individual `translate`
 * property). The 44px hit area is a pseudo-element so the visual track stays
 * compact while the touch target is comfortable.
 */
function Switch({
  className,
  size = "default",
  ...props
}: SwitchPrimitive.Root.Props & {
  size?: "sm" | "default"
}) {
  return (
    <SwitchPrimitive.Root
      data-slot="switch"
      data-size={size}
      className={cn("ui-switch", className)}
      {...props}
    >
      <SwitchPrimitive.Thumb data-slot="switch-thumb" className="ui-switch-thumb" />
    </SwitchPrimitive.Root>
  )
}

export { Switch }
