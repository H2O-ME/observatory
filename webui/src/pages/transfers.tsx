import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, RefreshCw, RotateCcw, Trash2 } from "lucide-react";

import { EmptyBlock, ErrorBlock, LoadingBlock } from "@/components/async-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { PageHeader } from "@/components/page-header";
import { Panel } from "@/components/panel";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { api, isApiError } from "@/lib/api";
import { serverTimeLeft } from "@/lib/clock";
import { formatBytes, formatDateTime } from "@/lib/format";
import type { FileTransfer } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Server-side staged file retrieval.
 *
 * A click on "下载" in the device's file browser does *not* stream bytes into
 * this tab: the server pulls the file from the agent into its own spool and
 * keeps it for 24 h. That is why this page — not the device page — is where
 * progress lives, and why closing the tab no longer cancels anything.
 */
const STATUS: Record<string, { label: string; variant: "default" | "secondary" | "success" | "neutral" | "destructive" }> = {
  pending: { label: "排队中", variant: "neutral" },
  running: { label: "取回中", variant: "default" },
  ready: { label: "可下载", variant: "success" },
  failed: { label: "失败", variant: "destructive" },
  cancelled: { label: "已取消", variant: "neutral" },
  expired: { label: "已过期", variant: "neutral" },
};

const ACTIVE = new Set(["pending", "running"]);

function percent(item: FileTransfer): number {
  if (item.expected_bytes <= 0) return 0;
  return Math.min(100, Math.round((item.received_bytes / item.expected_bytes) * 100));
}

function remaining(item: FileTransfer): string {
  // 服务端时钟（C5）：浏览器墙上时钟可能差几分钟，不能拿来算保留时间。
  const left = serverTimeLeft(item.expires_at_ms);
  if (left === null) return "";
  if (left <= 0) return "已过期";
  const hours = Math.floor(left / 3_600_000);
  const minutes = Math.floor((left % 3_600_000) / 60_000);
  return hours > 0 ? `${hours} 小时 ${minutes} 分` : `${minutes} 分`;
}

export function TransfersPage() {
  const queryClient = useQueryClient();
  const [pendingDelete, setPendingDelete] = useState<FileTransfer | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const query = useQuery({
    queryKey: ["file-transfers"],
    queryFn: api.listTransfers,
    // 1.5s only while something is moving; idle pages do not need to poll hard.
    refetchInterval: (current) => {
      const rows = current.state.data?.items ?? [];
      return rows.some((row) => ACTIVE.has(row.status)) ? 1500 : 15_000;
    },
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["file-transfers"] });
  };

  const remove = useMutation({
    mutationFn: (item: FileTransfer) => api.deleteTransfer(item.id),
    onSuccess: () => {
      setMessage("已删除取回任务与服务器上的暂存文件");
      setError("");
      setPendingDelete(null);
      invalidate();
    },
    onError: (failure) => setError(isApiError(failure) ? failure.message : "删除失败"),
  });

  const retry = useMutation({
    mutationFn: (item: FileTransfer) =>
      api.createTransfer(item.device_uid, item.path, item.expected_bytes),
    onSuccess: () => {
      setMessage("已重新加入取回队列");
      setError("");
      invalidate();
    },
    onError: (failure) => setError(isApiError(failure) ? failure.message : "重新取回失败"),
  });

  const items = query.data?.items ?? [];
  const busy = query.isFetching;

  return (
    <div className="w-full max-w-full min-w-0 space-y-5">
      <PageHeader
        title="文件取回"
        description="文件先由服务端从受控端取回并暂存 24 小时；关闭控制台、离开设备页或换设备都不影响，完成后从这里下载。"
        actions={
          <Button
            size="sm"
            variant="outline"
            className="w-full sm:w-auto"
            onClick={() => void query.refetch()}
            disabled={busy}
          >
            <RefreshCw className={cn("size-3.5", busy && "animate-spin")} aria-hidden />
            刷新
          </Button>
        }
      />

      {message ? (
        <div className="rounded-md border border-border bg-muted px-3 py-2 text-sm text-foreground">{message}</div>
      ) : null}
      {error ? <ErrorBlock message={error} /> : null}

      {query.isLoading ? (
        <LoadingBlock label="正在读取取回任务…" />
      ) : query.isError ? (
        <ErrorBlock
          message={isApiError(query.error) ? query.error.message : "无法读取取回任务"}
          retryable
          onRetry={() => void query.refetch()}
        />
      ) : items.length === 0 ? (
        <EmptyBlock
          title="暂无取回任务"
          description="在设备详情的「文件」页点「下载」，文件会先取回到服务器，再从这里下载。"
        />
      ) : (
        <Panel title="取回任务" hint="固定保留 24 小时，到期自动清理" flush>
          <ul className="divide-y divide-border">
            {items.map((item) => {
              const status = STATUS[item.status] ?? { label: item.status, variant: "neutral" as const };
              const active = ACTIVE.has(item.status);
              const left = item.status === "ready" ? remaining(item) : "";
              return (
                <li key={item.id} className="min-w-0 px-5 py-3 sm:px-6">
                  <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0 flex-1">
                      <div className="flex min-w-0 flex-wrap items-center gap-2">
                        <span className="min-w-0 truncate text-sm font-medium text-foreground">
                          {item.name}
                        </span>
                        <Badge variant={status.variant}>{status.label}</Badge>
                        {active ? (
                          <span className="tnum text-xs text-muted-foreground">{percent(item)}%</span>
                        ) : null}
                      </div>
                      <div className="mt-1 min-w-0 truncate text-xs text-muted-foreground">
                        {item.device_name || item.device_uid} · <span className="font-mono">{item.path}</span>
                      </div>
                      {active ? (
                        <Progress className="mt-2 max-w-md" value={percent(item)} aria-label="取回进度" />
                      ) : null}
                      <div className="mt-1.5 flex min-w-0 flex-wrap gap-x-3 gap-y-1 text-xs text-faint">
                        <span className="tnum">
                          {formatBytes(item.received_bytes)} / {formatBytes(item.expected_bytes)}
                        </span>
                        <span>发起 {formatDateTime(item.created_at_ms)}</span>
                        {item.status === "ready" && item.expires_at_ms ? (
                          <span>
                            保留至 {formatDateTime(item.expires_at_ms)}
                            {left ? `（剩 ${left}）` : ""}
                          </span>
                        ) : null}
                      </div>
                      {item.error ? (
                        <p className="mt-1.5 min-w-0 text-xs break-words text-destructive">{item.error}</p>
                      ) : null}
                    </div>

                    <div className="flex shrink-0 flex-wrap items-center gap-2">
                      {item.status === "ready" ? (
                        <a
                          href={api.transferContentUrl(item.id)}
                          className={cn(buttonVariants({ size: "sm" }), "no-underline")}
                        >
                          <Download className="size-3.5" aria-hidden />
                          下载
                        </a>
                      ) : null}
                      {item.status === "failed" || item.status === "cancelled" ? (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={retry.isPending}
                          onClick={() => {
                            setMessage("");
                            setError("");
                            retry.mutate(item);
                          }}
                        >
                          <RotateCcw className="size-3.5" aria-hidden />
                          重试
                        </Button>
                      ) : null}
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          setMessage("");
                          setError("");
                          setPendingDelete(item);
                        }}
                      >
                        <Trash2 className="size-3.5" aria-hidden />
                        {active ? "取消" : "删除"}
                      </Button>
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        </Panel>
      )}

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
        title={pendingDelete && ACTIVE.has(pendingDelete.status) ? "取消取回并删除？" : "删除暂存文件？"}
        description={
          pendingDelete
            ? `将删除「${pendingDelete.name}」的取回任务与服务器上的暂存副本，不可恢复。`
            : ""
        }
        confirmLabel="删除"
        busy={remove.isPending}
        onConfirm={() => {
          if (pendingDelete) remove.mutate(pendingDelete);
        }}
      />
    </div>
  );
}
