import { Skeleton as RadixSkeleton } from "@radix-ui/themes"
import { cn } from "@/lib/utils"

function Skeleton({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <RadixSkeleton
      data-slot="skeleton"
      className={cn("block animate-pulse rounded-md bg-sunken", className)}
      {...props}
    />
  )
}

export { Skeleton }
