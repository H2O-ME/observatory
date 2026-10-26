import { mergeProps } from "@base-ui/react/merge-props";
import { useRender } from "@base-ui/react/use-render";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

/**
 * shadcn/ui (base-nova) Badge plus the console's status variants.
 *
 * `success` / `warning` exist because device presence has four states; they use
 * the same size, radius and typography as the official variants so a row of
 * badges still reads as one system.
 */
const badgeVariants = cva(
  "group/badge inline-flex h-5 w-fit shrink-0 items-center justify-center gap-1 overflow-hidden rounded-md border border-transparent px-1.5 py-0.5 text-xs font-medium whitespace-nowrap select-none [&>svg]:pointer-events-none [&>svg]:size-3",
  {
    variants: {
      variant: {
        default: "bg-primary text-primary-foreground [a]:hover:bg-primary-hover",
        secondary: "bg-secondary text-secondary-foreground [a]:hover:bg-accent",
        outline: "border-border text-foreground [a]:hover:bg-accent",
        ghost: "text-muted-foreground [a]:hover:bg-accent [a]:hover:text-foreground",
        destructive:
          "bg-destructive-soft text-destructive [a]:hover:bg-destructive [a]:hover:text-destructive-foreground",
        success: "bg-success-soft text-success",
        warning: "bg-warning-soft text-warning",
        neutral: "bg-muted text-muted-foreground",
        link: "text-primary underline-offset-4 hover:underline",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

function Badge({
  className,
  variant = "default",
  render,
  ...props
}: useRender.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return useRender({
    defaultTagName: "span",
    props: mergeProps<"span">(
      {
        className: cn(badgeVariants({ variant }), className),
      },
      props,
    ),
    render,
    state: {
      slot: "badge",
      variant,
    },
  });
}

export { Badge, badgeVariants };
