import { Badge as RadixBadge } from "@radix-ui/themes"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "group/badge inline-flex h-5 w-fit shrink-0 items-center justify-center gap-1 overflow-hidden rounded-4xl px-2 py-0.5 text-xs font-medium whitespace-nowrap select-none [&>svg]:pointer-events-none [&>svg]:size-3!",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground",
        secondary: "bg-secondary text-secondary-foreground",
        destructive: "bg-destructive/10 text-destructive dark:bg-destructive/20",
        outline: "border-border text-foreground",
        ghost: "text-muted-foreground",
        link: "text-primary underline-offset-4 hover:underline",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

const RADIX_VARIANT = {
  default: "solid",
  secondary: "soft",
  destructive: "soft",
  outline: "surface",
  ghost: "soft",
  link: "soft",
} as const

export interface BadgeProps
  extends Omit<React.ComponentProps<typeof RadixBadge>, "variant">,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant = "default", ...props }: BadgeProps) {
  const v = (variant ?? "default") as keyof typeof RADIX_VARIANT
  return (
    <RadixBadge
      data-slot="badge"
      variant={RADIX_VARIANT[v]}
      color="indigo"
      className={cn(badgeVariants({ variant, className }))}
      {...props}
    />
  )
}

export { Badge, badgeVariants }
