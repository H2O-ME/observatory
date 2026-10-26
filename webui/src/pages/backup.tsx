import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ArrowDownToLine, Download, RefreshCw, Trash2 } from "lucide-react";

import { Banner, EmptyBlock, ErrorBlock, LoadingBlock, Pager } from "@/components/async-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { MetaChip, PageHeader } from "@/components/page-header";
import { Panel } from "@/components/panel";
import { Section } from "@/components/section";
import { StatCell, StatStrip } from "@/components/stat-card";
import { Button, buttonVariants } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useViewport } from "@/hooks/use-viewport";
import { api, isApiError } from "@/lib/api";
import { formatBytes, formatDateTime } from "@/lib/format";
import type { BackupEntry } from "@/lib/types";
import { cn } from "@/lib/utils";

const PAGE = 25;

/** Archive state → MetaChip tone. Only expresses state, never decoration. */
const STATUS_TONE: Record<string, "warn" | "ok" | "danger" | "default"> = {
  待上传: "warn",
  已上传: "ok",
  失败: "danger",
  已删除: "default",
};

/**
 * Archive / backup console (A14: the feature stays, so it needs a real page).
 *
 * States come from the media archive state machine; the page never derives
 * "backed up" from anything local. Deleting an entry removes the remote copy
 * first — the server only flips the status to 已删除 after the remote delete is
 * confirmed, and surfaces the failure otherwise.
 */
export function BackupPage() {
  const viewport = useViewport();
  const watch = viewport === "watch";
  const touch = viewport !== "desktop";
  const queryClient = useQueryClient();
  const [status, setStatus] = useState("");
  const [deviceUid, setDeviceUid] = useState("");
  const [offset, setOffset] = useState(0);
  const [pendingDelete, setPendingDelete] = useState<BackupEntry | null>(null);
  const [detailId, setDetailId] = useState<number | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const overview = useQuery({
    queryKey: ["backup-overview"],
    queryFn: api.backupOverview,
    refetchInterval: 20_000,
  });

  const entries = useQuery({
    queryKey: ["backup-entries", status, deviceUid, PAGE, offset],
    queryFn: () => api.listBackup({ status, deviceUid, limit: PAGE, offset }),
    refetchInterval: 15_000,
  });

  const devices = useQuery({ queryKey: ["devices"], queryFn: api.listDevices, staleTime: 60_000 });

  const detail = useQuery({
    queryKey: ["backup-detail", detailId ?? 0],
    queryFn: () => api.backupDetail(detailId!),
    enabled: detailId !== null,
  });

  const retrieve = useMutation({
    mutationFn: (entryId: number) => api.retrieveBackup(entryId),
    onSuccess: (result) => {
      setMessage(`已取回归档（帧 ${result.frame_id}），现在可以直接下载本地副本`);
      setError("");
      void queryClient.invalidateQueries({ queryKey: ["backup-entries"] });
      void queryClient.invalidateQueries({ queryKey: ["backup-overview"] });
      void queryClient.invalidateQueries({ queryKey: ["backup-detail"] });
    },
    onError: (failure) => setError(isApiError(failure) ? failure.message : "取回失败"),
  });

  const remove = useMutation({
    mutationFn: (entryId: number) => api.deleteBackup(entryId),
    onSuccess: (result) => {
      setMessage(result.message);
      setError("");
      setPendingDelete(null);
      void queryClient.invalidateQueries({ queryKey: ["backup-entries"] });
      void queryClient.invalidateQueries({ queryKey: ["backup-overview"] });
    },
    onError: (failure) => {
      setError(isApiError(failure) ? failure.message : "删除失败");
      setPendingDelete(null);
    },
  });

  const summary = overview.data?.summary;
  const data = entries.data;
  const configured = overview.data?.configured ?? false;
  const busy = overview.isFetching || entries.isFetching;

  const changeFilter = (next: () => void) => {
    next();
    setOffset(0);
  };

  const statuses = data?.statuses ?? overview.data?.statuses ?? [];
  const failures = overview.data?.recent_failures ?? [];
  const tapClass = touch ? "min-h-11 min-w-11" : undefined;

  return (
    <div className={cn("w-full min-w-0 space-y-5", watch && "space-y-3")}>
      <PageHeader
        title="归档备份"
        description={
          overview.data
            ? overview.data.configured
              ? "远端归档状态与上传队列；删除会先删远端副本，成功后才标记为已删除。"
              : "未配置远端归档仓库；请在设置中填写仓库与分支。"
            : "正在读取归档状态…"
        }
        meta={
          overview.data ? (
            overview.data.configured ? (
              <MetaChip tone="brand" className="min-w-0">
                <span className="min-w-0 truncate font-mono">
                  {overview.data.repo || "（仓库未填）"}
                  {overview.data.branch ? ` · ${overview.data.branch}` : ""}
                </span>
              </MetaChip>
            ) : (
              <MetaChip tone="warn">未配置仓库</MetaChip>
            )
          ) : null
        }
        actions={
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              void overview.refetch();
              void entries.refetch();
            }}
            disabled={busy}
            className={cn("w-full sm:w-auto", touch && "min-h-11")}
          >
            <RefreshCw className={cn("size-3.5", busy && "animate-spin")} aria-hidden />
            刷新
          </Button>
        }
      />

      {message ? <Banner tone="ok">{message}</Banner> : null}
      {error ? <Banner tone="error">{error}</Banner> : null}

      {overview.isError ? (
        <ErrorBlock
          message={isApiError(overview.error) ? overview.error.message : "无法读取归档状态"}
          retryable
          onRetry={() => void overview.refetch()}
        />
      ) : null}

      {!overview.isLoading && !configured ? (
        <EmptyBlock
          title="远端归档仓库尚未配置"
          description="在设置里填写 archive_enabled / archive_repo / archive_branch 后才会开始上传；未配置时画面只保留在本地。"
          action={
            <Link to="/settings" className={cn(buttonVariants({ variant: "outline", size: "sm" }), "no-underline")}>
              去设置归档
            </Link>
          }
        />
      ) : null}

      {summary && configured ? (
        <div className="grid min-w-0 gap-2">
          <StatStrip>
            <StatCell
              label="待上传"
              value={summary.statuses["待上传"] ?? summary.pending ?? 0}
              hint="等待进入归档队列"
              tone="warn"
            />
            <StatCell
              label="已上传"
              value={summary.statuses["已上传"] ?? summary.uploaded ?? 0}
              hint="远端副本可用"
            />
            <StatCell
              label="失败"
              value={summary.statuses["失败"] ?? summary.failed ?? 0}
              hint="服务端会自动重试"
              tone={summary.statuses["失败"] || summary.failed ? "danger" : "neutral"}
            />
            <StatCell
              label="已删除"
              value={summary.statuses["已删除"] ?? summary.deleted ?? 0}
              hint="远端副本已清除"
            />
          </StatStrip>
          <p className="min-w-0 break-words px-1 text-xs leading-relaxed text-faint">
            队列 {summary.queue_size ?? 0} · worker {summary.worker_running ? "运行中" : "未运行"} · 本次入队{" "}
            {summary.enqueued ?? 0} · 已上传 {summary.uploaded ?? 0} · 失败 {summary.failed ?? 0}
            {summary.verify_failed > 0 ? ` · 校验失败 ${summary.verify_failed}` : ""}
            {summary.last_error ? (
              <span className="text-destructive"> · 最近错误：{summary.last_error}</span>
            ) : null}
          </p>
        </div>
      ) : null}

      {failures.length > 0 ? (
        <Section
          tone="warn"
          title={`最近未成功的归档（${failures.length}）`}
          hint="服务端会自动重试；超过上限后需排查仓库权限或网络。"
          flush
        >
          <ul className="divide-y divide-border">
            {failures.map((failure) => (
              <li
                key={failure.id}
                className="grid min-w-0 grid-cols-1 gap-1 px-6 py-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,18rem)] sm:items-center sm:gap-3"
              >
                <span className="min-w-0 truncate font-mono text-xs text-foreground">{failure.remote_path}</span>
                <span className="min-w-0 break-words text-xs text-warning">
                  {failure.status} · 尝试 {failure.attempts} 次
                  {failure.error ? ` · ${failure.error}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Select
          value={status || null}
          onValueChange={(value) => changeFilter(() => setStatus(typeof value === "string" ? value : ""))}
        >
          <SelectTrigger size="sm" className={cn("w-full sm:w-44", touch && "min-h-11")} aria-label="按归档状态筛选">
            <SelectValue placeholder="全部状态" />
          </SelectTrigger>
          <SelectContent>
            {statuses.map((item) => (
              <SelectItem key={item} value={item}>
                {item}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Select
          value={deviceUid || null}
          onValueChange={(value) => changeFilter(() => setDeviceUid(typeof value === "string" ? value : ""))}
        >
          <SelectTrigger size="sm" className={cn("w-full sm:w-52", touch && "min-h-11")} aria-label="按设备筛选归档">
            <SelectValue placeholder="全部设备" />
          </SelectTrigger>
          <SelectContent>
            {(devices.data?.devices ?? []).map((device) => (
              <SelectItem key={device.uid} value={device.uid}>
                {device.name || device.uid}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {status || deviceUid ? (
          <Button
            size="sm"
            variant="ghost"
            className={cn(tapClass)}
            onClick={() =>
              changeFilter(() => {
                setStatus("");
                setDeviceUid("");
              })
            }
          >
            清除筛选
          </Button>
        ) : null}

        <span className="ml-auto tnum hidden text-xs text-faint sm:inline">
          {data ? `${data.total} 条归档` : ""}
        </span>
      </div>

      {entries.isError ? (
        <ErrorBlock
          message={isApiError(entries.error) ? entries.error.message : "无法读取归档列表"}
          retryable
          onRetry={() => void entries.refetch()}
        />
      ) : entries.isLoading ? (
        <LoadingBlock label="正在读取归档列表…" rows={5} />
      ) : !data || data.entries.length === 0 ? (
        configured ? (
          <EmptyBlock
            title="没有归档记录"
            description="归档会在画面采集后被异步上传；如果刚开启归档，稍等片刻或检查队列状态。"
            action={
              <Link to="/settings" className={cn(buttonVariants({ variant: "outline", size: "sm" }), "no-underline")}>
                去设置归档
              </Link>
            }
          />
        ) : null
      ) : (
        <Panel
          flush
          className="min-w-0"
          title="归档记录"
          hint="点击一条记录查看远端路径与校验值"
          actions={<span className="tnum text-xs text-faint">共 {data.total} 条</span>}
        >
          <ul className="relative min-w-0 divide-y divide-border">
            {data.entries.map((entry) => (
              <li
                key={entry.id}
                className="grid min-w-0 grid-cols-1 gap-2 px-6 py-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:gap-3"
              >
                <button
                  type="button"
                  onClick={() => setDetailId(entry.id)}
                  className="grid min-w-0 grid-cols-1 gap-1 rounded-sm text-left transition-colors hover:bg-muted"
                  title="查看归档详情"
                >
                  <span className="flex min-w-0 flex-wrap items-center">
                    <MetaChip tone={STATUS_TONE[entry.status] ?? "default"} className="mr-2 shrink-0">
                      {entry.status}
                    </MetaChip>
                    <span className="min-w-0 truncate font-mono text-sm font-medium text-foreground">
                      {entry.device_name || entry.device_uid || `设备 ${entry.device_id}`}
                    </span>
                  </span>
                  <span className="grid min-w-0 gap-x-3 gap-y-0.5 text-xs text-faint sm:grid-cols-2">
                    <span className="min-w-0 truncate">
                      {entry.day} · {formatBytes(entry.size_bytes)}
                    </span>
                    {!watch ? (
                      <span className="min-w-0 truncate">
                        {entry.frame_id ?? "—"} · {entry.local_available ? "本地副本在" : "仅远端"} · 尝试{" "}
                        {entry.attempts} 次
                      </span>
                    ) : null}
                    {entry.error ? (
                      <span className="min-w-0 truncate text-destructive sm:col-span-2">{entry.error}</span>
                    ) : null}
                  </span>
                </button>

                <div className="flex min-w-0 flex-wrap items-center justify-start sm:justify-end">
                  {entry.local_available ? (
                    <a
                      href={api.backupFileUrl(entry.id)}
                      className={cn(buttonVariants({ variant: "ghost", size: "sm" }), "no-underline", tapClass)}
                      title="下载本地副本"
                      aria-label="下载本地副本"
                    >
                      <Download className="size-4" aria-hidden />
                      <span className="ml-1.5 hidden sm:inline">下载</span>
                    </a>
                  ) : (
                    <Button
                      size="sm"
                      variant="ghost"
                      className={tapClass}
                      title="从远端取回到本地"
                      aria-label="从远端取回到本地"
                      disabled={retrieve.isPending || !configured}
                      onClick={() => retrieve.mutate(entry.id)}
                    >
                      <ArrowDownToLine className="size-4" aria-hidden />
                      <span className="ml-1.5 hidden sm:inline">取回</span>
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="ghost"
                    className={cn("text-muted-foreground hover:text-destructive", tapClass)}
                    title="删除远端归档（先删远端，成功后才标记已删除）"
                    aria-label="删除远端归档"
                    disabled={remove.isPending || entry.status === "已删除"}
                    onClick={() => setPendingDelete(entry)}
                  >
                    <Trash2 className="size-4" aria-hidden />
                    <span className="ml-1.5 hidden sm:inline">删除</span>
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {data ? <Pager total={data.total} limit={PAGE} offset={offset} onChange={setOffset} busy={entries.isFetching} /> : null}

      <Dialog
        open={detailId !== null}
        onOpenChange={(open) => {
          if (!open) setDetailId(null);
        }}
      >
        <DialogContent size="lg" className="max-h-[88vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>归档详情 #{detailId ?? ""}</DialogTitle>
            <DialogDescription>远端路径、校验值与本地副本状态</DialogDescription>
          </DialogHeader>
          {detail.isLoading ? (
            <LoadingBlock label="正在读取归档详情…" />
          ) : detail.isError ? (
            <ErrorBlock
              message={isApiError(detail.error) ? detail.error.message : "无法读取归档详情"}
              retryable
              onRetry={() => void detail.refetch()}
            />
          ) : detail.data ? (
            <div className="grid min-w-0 gap-3">
              {detail.data.entry.local_available && detail.data.entry.frame ? (
                <div className="ratio-16-10 w-full overflow-hidden rounded-xl border border-border bg-black">
                  <img
                    src={api.frameUrl(detail.data.entry.device_uid, detail.data.entry.frame.id, "full")}
                    alt="归档画面"
                    className="absolute-fill h-full w-full object-contain"
                  />
                </div>
              ) : null}

              <dl className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-xs">
                <dt className="text-faint">设备</dt>
                <dd className="min-w-0 break-words text-muted-foreground">
                  {detail.data.entry.device_name || detail.data.entry.device_uid || detail.data.entry.device_id}
                </dd>
                <dt className="text-faint">日期</dt>
                <dd className="min-w-0 break-words text-muted-foreground">{detail.data.entry.day}</dd>
                <dt className="text-faint">远端路径</dt>
                <dd className="min-w-0 break-all font-mono text-xs text-muted-foreground">{detail.data.entry.remote_path}</dd>
                <dt className="text-faint">sha256</dt>
                <dd className="min-w-0 break-all font-mono text-xs text-muted-foreground">{detail.data.entry.sha256 || "—"}</dd>
                <dt className="text-faint">大小</dt>
                <dd className="min-w-0 break-words text-muted-foreground">{formatBytes(detail.data.entry.size_bytes)}</dd>
                <dt className="text-faint">状态</dt>
                <dd className="min-w-0 break-words text-muted-foreground">
                  {detail.data.entry.status} · 尝试 {detail.data.entry.attempts} 次
                </dd>
                <dt className="text-faint">创建</dt>
                <dd className="min-w-0 break-words text-muted-foreground">{formatDateTime(detail.data.entry.created_at_ms)}</dd>
                <dt className="text-faint">上传</dt>
                <dd className="min-w-0 break-words text-muted-foreground">{formatDateTime(detail.data.entry.uploaded_at_ms ?? 0)}</dd>
                <dt className="text-faint">本地副本</dt>
                <dd className="min-w-0 break-words text-muted-foreground">{detail.data.entry.local_available ? "在" : "仅远端"}</dd>
              </dl>
              {detail.data.entry.error ? <Banner tone="error">{detail.data.entry.error}</Banner> : null}
            </div>
          ) : null}
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
        title="删除远端归档"
        description={
          pendingDelete
            ? `将从远端仓库删除 ${pendingDelete.remote_path}。本地画面不受影响；远端删除成功前状态不会变成“已删除”。`
            : ""
        }
        confirmLabel="删除远端副本"
        busy={remove.isPending}
        onConfirm={() => {
          if (pendingDelete) remove.mutate(pendingDelete.id);
        }}
      />
    </div>
  );
}
