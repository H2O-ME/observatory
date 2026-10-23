import type { ReactNode } from "react";
import { AlertTriangle, Inbox, Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Shared page furniture: one header, one error banner, one empty state, one
 * loading block. Seven pages reuse these so "loading / empty / error" cannot be
 * forgotten on one of them (the 0.2.x pages each invented their own, and half of
 * them had no empty state at all).
 */

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-2">
      <div className="min-w-0">
        <h1 className="text-base font-semibold text-ink">{title}</h1>
        {subtitle ? <p className="mt-0.5 text-[0.72rem] leading-relaxed text-soft">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export function Banner({
  tone = "info",
  children,
  className,
}: {
  tone?: "info" | "ok" | "warn" | "error";
  children: ReactNode;
  className?: string;
}) {
  const tones: Record<string, string> = {
    info: "border-line bg-sunken text-soft",
    ok: "border-ok/30 bg-ok/10 text-ok",
    warn: "border-warn/40 bg-warn/10 text-warn",
    error: "border-danger/30 bg-danger/10 text-danger",
  };
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      aria-live="polite"
      className={cn("rounded-md border px-2.5 py-1.5 text-[0.72rem] leading-relaxed", tones[tone], className)}
    >
      {children}
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
    <div className="flex flex-wrap items-center gap-2 rounded-panel border border-danger/30 bg-danger/10 p-3 text-xs text-danger">
      <AlertTriangle className="size-4 shrink-0" aria-hidden />
      <span className="min-w-0 flex-1">{message}</span>
      {retryable && onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="rounded-md border border-danger/40 px-2 py-1 text-[0.7rem] text-danger hover:bg-danger/10"
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
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 rounded-panel border border-dashed border-line px-4 py-10 text-center">
      <Inbox className="size-6 text-faint" aria-hidden />
      <p className="text-sm font-medium text-ink">{title}</p>
      {description ? <p className="max-w-md text-[0.72rem] leading-relaxed text-soft">{description}</p> : null}
      {action ? <div className="mt-1">{action}</div> : null}
    </div>
  );
}

export function LoadingBlock({ label = "加载中…", rows = 3 }: { label?: string; rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-label={label}>
      <p className="flex items-center gap-1.5 text-[0.72rem] text-soft">
        <Loader2 className="size-3.5 animate-spin" /> {label}
      </p>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="h-9 w-full animate-pulse rounded-md bg-sunken" />
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
    <div className="flex flex-wrap items-center justify-between gap-2 text-[0.72rem] text-soft">
      <span>
        共 {total} 条 · 第 {page} / {pages} 页
      </span>
      <div className="flex items-center gap-1.5">
        <button
          type="button"
          disabled={busy || offset <= 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
          className="rounded-md border border-line px-2 py-1 disabled:opacity-40"
        >
          上一页
        </button>
        <button
          type="button"
          disabled={busy || offset + limit >= total}
          onClick={() => onChange(offset + limit)}
          className="rounded-md border border-line px-2 py-1 disabled:opacity-40"
        >
          下一页
        </button>
      </div>
    </div>
  );
}