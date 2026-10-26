import type { ComponentType, ReactNode } from "react";
import { AlertTriangle, Inbox } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";

/* Re-export for older call sites; new code should import PageHeader directly. */
export { PageHeader } from "@/components/page-header";

const BANNER_VARIANT: Record<"info" | "ok" | "warn" | "error", "info" | "success" | "warning" | "destructive"> = {
  info: "info",
  ok: "success",
  warn: "warning",
  error: "destructive",
};

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
  return (
    <Alert
      variant={BANNER_VARIANT[tone]}
      role={tone === "error" ? "alert" : "status"}
      aria-live="polite"
      className={cn("flex min-w-0 items-start gap-2", className)}
    >
      {Icon ? <Icon className="mt-0.5 size-4 shrink-0" aria-hidden /> : null}
      <div className="min-w-0 flex-1">
        {title ? <AlertTitle>{title}</AlertTitle> : null}
        <AlertDescription className={cn(title && "mt-0.5")}>{children}</AlertDescription>
      </div>
    </Alert>
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
    <Alert variant="destructive" className="flex min-w-0 flex-wrap items-center gap-2 text-sm">
      <AlertTriangle className="size-4 shrink-0" aria-hidden />
      <span className="min-w-0 flex-1 break-words">{message}</span>
      {retryable && onRetry ? (
        <Button variant="outline" size="xs" onClick={onRetry}>
          重试
        </Button>
      ) : null}
    </Alert>
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
  return (
    <Empty className={cn("bg-card", className)}>
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <Icon />
        </EmptyMedia>
        <EmptyTitle>{title}</EmptyTitle>
        {description ? <EmptyDescription>{description}</EmptyDescription> : null}
      </EmptyHeader>
      {action ? <EmptyContent>{action}</EmptyContent> : null}
    </Empty>
  );
}

export function LoadingBlock({ label = "加载中…", rows = 3 }: { label?: string; rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-label={label}>
      {label ? (
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Spinner className="size-3.5 text-primary" /> {label}
        </p>
      ) : null}
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="h-11 w-full rounded-md" />
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
    <div className="flex min-w-0 flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
      <span className="tnum">
        共 {total} 条 · 第 {page} / {pages} 页
      </span>
      <div className="flex items-center gap-1.5">
        <Button
          variant="outline"
          size="sm"
          disabled={busy || offset <= 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
        >
          上一页
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={busy || offset + limit >= total}
          onClick={() => onChange(offset + limit)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}
