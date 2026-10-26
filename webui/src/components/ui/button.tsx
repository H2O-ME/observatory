import * as React from "react"
import { Button as RadixButton } from "@radix-ui/themes"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

/**
 * Radix Themes 迁移（0.3.8）：结构/焦点/禁用语义交给 @radix-ui/themes 的
 * Button，本项目变体映射到它的 variant/size；语义令牌类（bg-brand、
 * border-line 等）继续作为视觉层叠在上面——Tailwind utilities 层声明在
 * radix 主题层之后，同特异性下 utilities 胜出，页面外观保持连贯。
 */
const buttonVariants = cva(
  "group/button inline-flex shrink-0 items-center justify-center whitespace-nowrap outline-none select-none disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default: "bg-brand text-brand-ink hover:brightness-110",
        outline: "border-line bg-sunken text-ink hover:border-line-strong hover:bg-panel",
        secondary: "border-line bg-panel text-ink hover:bg-panel-strong",
        ghost: "text-soft hover:bg-sunken hover:text-ink",
        destructive: "border-line bg-danger-soft text-danger hover:brightness-110",
        link: "text-brand underline-offset-4 hover:underline",
      },
      size: {
        default: "h-10 gap-1.5 rounded-xl px-4 text-sm font-semibold",
        xs: "h-8 gap-1 rounded-lg px-2.5 text-xs font-medium [&_svg:not([class*='size-'])]:size-3",
        sm: "h-9 gap-1.5 rounded-lg px-3 text-[0.82rem] font-medium [&_svg:not([class*='size-'])]:size-4",
        lg: "h-11 gap-2 rounded-xl px-5 text-[0.92rem] font-semibold",
        icon: "size-10 rounded-xl",
        "icon-xs": "size-8 rounded-lg [&_svg:not([class*='size-'])]:size-3.5",
        "icon-sm": "size-9 rounded-lg",
        "icon-lg": "size-11 rounded-xl",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

type OurVariant = NonNullable<VariantProps<typeof buttonVariants>["variant"]>
type OurSize = NonNullable<VariantProps<typeof buttonVariants>["size"]>

/** 本项目变体 → Radix Themes 变体（贡献 radius/焦点环等主题默认值）。 */
const RADIX_VARIANT: Record<OurVariant, "solid" | "surface" | "soft" | "ghost"> = {
  default: "solid",
  outline: "surface",
  secondary: "surface",
  ghost: "ghost",
  destructive: "soft",
  link: "ghost",
}

const RADIX_SIZE: Record<OurSize, "1" | "2" | "3"> = {
  xs: "1",
  sm: "1",
  default: "2",
  icon: "2",
  "icon-xs": "1",
  "icon-sm": "1",
  "icon-lg": "3",
  lg: "3",
}

export interface ButtonProps
  extends Omit<React.ComponentProps<typeof RadixButton>, "variant" | "size">,
    VariantProps<typeof buttonVariants> {}

function Button({ className, variant: v = "default", size: sz = "default", ...props }: ButtonProps) {
  const variant = (v ?? "default") as OurVariant
  const size = (sz ?? "default") as OurSize
  return (
    <RadixButton
      data-slot="button"
      variant={RADIX_VARIANT[variant]}
      size={RADIX_SIZE[size]}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }
