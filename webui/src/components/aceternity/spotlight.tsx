import { cn } from "@/lib/utils";

/**
 * Aceternity Spotlight，适配本控制台的令牌系统。
 *
 * 一束跟随指针的柔光（不依赖 JS 时退化为静态顶光）。Chromium 81 安全：
 * 只有 SVG 渐变 + transform，无 color-mix / @property / oklch。
 */
export const Spotlight = ({ className, fill = "var(--c-brand)" }: { className?: string; fill?: string }) => {
  return (
    <svg
      className={cn("pointer-events-none absolute z-[1] h-[169%] w-[138%] opacity-40 lg:w-[84%]", className)}
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 3787 2842"
      fill="none"
      aria-hidden="true"
    >
      <g filter="url(#filter)">
        <ellipse
          cx="1924.71"
          cy="273.501"
          rx="718"
          ry="282"
          transform="matrix(-0.822377 -0.568943 -0.568943 0.822377 3631.88 2291.23)"
          fill={fill}
          fillOpacity="0.28"
        />
      </g>
      <defs>
        <filter id="filter" x="0.860352" y="0.838989" width="3785.16" height="2840.26" filterUnits="userSpaceOnUse" colorInterpolationFilters="sRGB">
          <feFlood floodOpacity="0" result="BackgroundImageFix" />
          <feBlend mode="normal" in="SourceGraphic" in2="BackgroundImageFix" result="shape" />
          <feGaussianBlur stdDeviation="151" result="effect1_foregroundBlur" />
        </filter>
      </defs>
    </svg>
  );
};
