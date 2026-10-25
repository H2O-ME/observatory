import type { ComponentType, ReactNode } from "react";
import { AlertTriangle, Inbox, Loader2 } from "lucide-react";

import { GridPattern } from "@/components/aceternity/grid-pattern";
import { useViewport } from "@/hooks/use-viewport";
import { cn } from "@/lib/utils";

/* Re-export for older call sites; new code should import PageHeader directly. */
export { PageHeader } from "@/components/page-header";

export function Banner({
  tone = "info",
  children,
  className,
  title,
  icon: Icon,
}: {
  tone?: "info" | "ok" | "warn" | "error";
  children: ReactNode;
  className?: string;
  title?: ReactNode;
  icon?: ComponentType<{ className?: string }>;
}) {
  const tones: Record<string, string> = {
    info: "surface-quiet text-soft",
    ok: "surface-ok text-ok",
    warn: "surface-warn text-warn",
    error: "surface-danger text-danger",
  };
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      aria-live="polite"
      className={cn("flex min-w-0 items-start gap-2 px-3 py-2.5 text-[0.8rem] leading-relaxed", tones[tone], className)}
    >
      {Icon ? <Icon className="mt-0.5 size-4 shrink-0" aria-hidden /> : null}
      <div className="min-w-0 flex-1">
        {title ? <p className="font-semibold">{title}</p> : null}
        <div className={cn(title && "mt-0.5 font-normal")}>{children}</div>
      </div>
    </div>
  );
}

export function ErrorBlock({
  message,
  retryable,
  onRetry,
}: {
  message: string;
  retryable?: boolean;
  onRetry?: () => void;
}) {
  return (
    <div className="surface-danger animate-sm-in flex min-w-0 flex-wrap items-center gap-2 p-3.5 text-[0.82rem] text-danger">
      <AlertTriangle className="size-4 shrink-0" aria-hidden />
      <span className="min-w-0 flex-1 break-words">{message}</span>
      {retryable && onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="tap rounded-lg border border-line bg-sunken px-3 py-1 text-[0.78rem] font-medium text-danger"
        >
          重试
        </button>
      ) : null}
    </div>
  );
}

export function EmptyBlock({
  title,
  description,
  action,
  icon: Icon = Inbox,
  className,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  icon?: ComponentType<{ className?: string }>;
  className?: string;
}) {
  const viewport = useViewport();
  const watch = viewport === "watch";
  return (
    <div
      className={cn(
        "surface relative flex flex-col items-center justify-center overflow-hidden text-center",
        watch ? "px-3 py-7" : "px-6 py-12",
        className,
      )}
    >
      <GridPattern
        className="pointer-events-none absolute inset-full opacity-40"
        stroke="var(--c-grid-line)"
        strokeDasharray="4 6"
      />
      <span
        className={cn(
          "relative flex items-center justify-center rounded-2xl border border-line bg-brand-soft text-brand",
          watch ? "size-10" : "size-12",
        )}
      >
        <Icon className={watch ? "size-5" : "size-6"} />
      </span>
      <p className={cn("relative mt-3 font-semibold text-ink", watch ? "text-[0.85rem]" : "text-[0.95rem]")}>{title}</p>
      {description ? (
        <p
          className={cn(
            "relative mt-1 max-w-md text-soft",
            watch ? "text-[0.7rem] leading-relaxed" : "text-[0.8rem] leading-relaxed",
          )}
        >
          {description}
        </p>
      ) : null}
      {action ? <div className="relative mt-4">{action}</div> : null}
    </div>
  );
}

export function LoadingBlock({ label = "加载中…", rows = 3 }: { label?: string; rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-label={label}>
      {label ? (
        <p className="flex items-center gap-1.5 text-[0.78rem] text-soft">
          <Loader2 className="size-3.5 animate-spin text-brand" /> {label}
        </p>
      ) : null}
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="h-11 w-full animate-pulse rounded-xl border border-line bg-sunken" />
      ))}
    </div>
  );
}

/** Pager used by the audit, backup and command lists (server-side limit/offset). */
export function Pager({
  total,
  limit,
  offset,
  onChange,
  busy,
}: {
  total: number;
  limit: number;
  offset: number;
  onChange: (offset: number) => void;
  busy?: boolean;
}) {
  const pages = Math.max(1, Math.ceil(total / limit));
  const page = Math.floor(offset / limit) + 1;
  return (
    <div className="flex min-w-0 flex-wrap items-center justify-between gap-2 text-[0.78rem] text-soft">
      <span className="tnum">
        共 {total} 条 · 第 {page} / {pages} 页
      </span>
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          disabled={busy || offset <= 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
          className="tap rounded-lg border border-line bg-sunken px-3 py-1.5 font-medium disabled:opacity-40"
        >
          上一页
        </button>
        <button
          type="button"
          disabled={busy || offset + limit >= total}
          onClick={() => onChange(offset + limit)}
          className="tap rounded-lg border border-line bg-sunken px-3 py-1.5 font-medium disabled:opacity-40"
        >
          下一页
        </button>
      </div>
    </div>
  );
}
