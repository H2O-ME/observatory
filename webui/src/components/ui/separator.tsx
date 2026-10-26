import { Separator as RadixSeparator } from "@radix-ui/themes"
import { cn } from "@/lib/utils"

function Separator({
  className,
  orientation = "horizontal",
  ...props
}: React.ComponentProps<typeof RadixSeparator>) {
  return (
    <RadixSeparator
      data-slot="separator"
      orientation={orientation}
      color="gray"
      className={cn(
        "shrink-0 bg-line",
        orientation === "horizontal" ? "h-px w-full" : "w-px self-stretch",
        className
      )}
      {...props}
    />
  )
}

export { Separator }
