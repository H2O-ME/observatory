import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { AlertTriangle, ArrowDownToLine, CloudUpload, Database, Download, RefreshCw, Trash2 } from "lucide-react";

import { Banner, EmptyBlock, ErrorBlock, LoadingBlock, PageHeader, Pager } from "@/components/async-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { Button, buttonVariants } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api, isApiError } from "@/lib/api";
import { formatBytes, formatDateTime } from "@/lib/format";
import type { BackupEntry } from "@/lib/types";
import { cn } from "@/lib/utils";

const PAGE = 25;

const STATUS_TONE: Record<string, string> = {
  待上传: "border-warn/40 bg-warn/10 text-warn",
  已上传: "border-ok/30 bg-ok/10 text-ok",
  失败: "border-danger/30 bg-danger/10 text-danger",
  已删除: "border-line bg-sunken text-faint",
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

  const changeFilter = (next: () => void) => {
    next();
    setOffset(0);
  };

  return (
    <div className="space-y-3">
      <PageHeader
        title="归档备份"
        subtitle={
          overview.data
            ? overview.data.configured
              ? `${overview.data.repo || "（仓库未填）"}${overview.data.branch ? ` · ${overview.data.branch}` : ""} · ${
                  overview.data.archive_enabled ? "自动归档开启" : "自动归档关闭"
                }`
              : "未配置远端归档仓库"
            : "读取中…"
        }
        actions={
          <Button
            size="sm"
            variant="outline"
            onClick={() => {
              void overview.refetch();
              void entries.refetch();
            }}
            disabled={overview.isFetching || entries.isFetching}
          >
            <RefreshCw className={cn("size-3.5", (overview.isFetching || entries.isFetching) && "animate-spin")} />
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
          description="归档功能保留（A14），但需要在设置里填写 archive_enabled / archive_repo / archive_branch（GitHub 仓库与分支）后才会开始上传。未配置时画面只保留在本地。"
        />
      ) : null}

      {summary && configured ? (
        <section className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {[
            { label: "待上传", value: summary.statuses["待上传"] ?? summary.pending ?? 0, tone: "text-warn" },
            { label: "已上传", value: summary.statuses["已上传"] ?? summary.uploaded ?? 0, tone: "text-ok" },
            { label: "失败", value: summary.statuses["失败"] ?? summary.failed ?? 0, tone: "text-danger" },
            { label: "已删除", value: summary.statuses["已删除"] ?? summary.deleted ?? 0, tone: "text-faint" },
          ].map((item) => (
            <div key={item.label} className="rounded-panel border border-line bg-panel p-2.5">
              <p className="text-[0.66rem] text-faint">{item.label}</p>
              <p className={cn("mt-0.5 font-mono text-lg font-semibold", item.tone)}>{item.value}</p>
            </div>
          ))}
        </section>
      ) : null}

      {summary && configured ? (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-panel border border-line bg-panel px-2.5 py-2 text-[0.68rem] text-soft">
          <span className="flex items-center gap-1">
            <CloudUpload className="size-3.5 text-faint" />
            队列 {summary.queue_size ?? 0} · worker {summary.worker_running ? "运行中" : "未运行"}
          </span>
          <span>本次进程已入队 {summary.enqueued ?? 0} · 已上传 {summary.uploaded ?? 0} · 失败 {summary.failed ?? 0}</span>
          {summary.last_error ? <span className="text-danger">最近错误：{summary.last_error}</span> : null}
        </div>
      ) : null}

      {overview.data && overview.data.recent_failures.length > 0 ? (
        <section className="rounded-panel border border-warn/40 bg-warn/10 p-3">
          <h2 className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-warn">
            <AlertTriangle className="size-3.5" aria-hidden />
            最近未成功的归档（{overview.data.recent_failures.length}）
          </h2>
          <ul className="space-y-1 text-[0.68rem] text-soft">
            {overview.data.recent_failures.map((failure) => (
              <li key={failure.id} className="flex items-start justify-between gap-2">
                <span className="min-w-0 flex-1 truncate font-mono text-[0.64rem]">{failure.remote_path}</span>
                <span className="shrink-0 text-warn">
                  {failure.status} · 尝试 {failure.attempts} 次
                  {failure.error ? ` · ${failure.error}` : ""}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-1.5 text-[0.64rem] leading-relaxed text-faint">
            失败行由服务端周期任务自动重试；超过重试上限后保持“失败”，需要人工排查仓库权限或网络。
          </p>
        </section>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={status || null}
          onValueChange={(value) => changeFilter(() => setStatus(typeof value === "string" ? value : ""))}
        >
          <SelectTrigger size="sm" className="w-32">
            <SelectValue placeholder="全部状态" />
          </SelectTrigger>
          <SelectContent>
            {(data?.statuses ?? overview.data?.statuses ?? []).map((item) => (
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
          <SelectTrigger size="sm" className="w-40">
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
        <EmptyBlock
          title="没有归档记录"
          description={
            configured
              ? "归档会在画面采集后被异步上传；如果刚开启归档，稍等片刻或检查队列状态。"
              : "先配置归档仓库，采集到的画面才会被上传。"
          }
          action={
            <Link to="/settings" className={cn(buttonVariants({ variant: "outline", size: "sm" }), "no-underline")}>
              <Database className="size-3.5" />
              去设置归档
            </Link>
          }
        />
      ) : (
        <ul className="divide-y divide-line rounded-panel border border-line bg-panel">
          {data.entries.map((entry) => (
            <li key={entry.id} className="flex flex-wrap items-start gap-2 px-2.5 py-2">
              <button
                type="button"
                onClick={() => setDetailId(entry.id)}
                className="min-w-0 flex-1 text-left"
                title="查看归档详情"
              >
                <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className={cn("rounded border px-1.5 py-0.5 text-[0.62rem]", STATUS_TONE[entry.status] ?? "border-line bg-sunken text-soft")}>
                    {entry.status}
                  </span>
                  <span className="truncate font-mono text-[0.72rem] text-ink">{entry.device_uid || `设备 ${entry.device_id}`}</span>
                  {entry.device_name ? <span className="text-[0.68rem] text-soft">{entry.device_name}</span> : null}
                </span>
                <span className="mt-0.5 block truncate text-[0.64rem] text-faint">
                  {entry.day} · {formatBytes(entry.size_bytes)} · {entry.frame_id ?? "—"} ·{" "}
                  {entry.local_available ? "本地副本在" : "仅远端"} · 尝试 {entry.attempts} 次
                </span>
                {entry.error ? <span className="mt-0.5 block truncate text-[0.64rem] text-danger">{entry.error}</span> : null}
              </button>
              <span className="flex shrink-0 items-center gap-1">
                {entry.local_available ? (
                  <a
                    href={api.backupFileUrl(entry.id)}
                    className={cn(buttonVariants({ variant: "ghost", size: "sm" }), "no-underline")}
                    title="下载本地副本"
                  >
                    <Download className="size-3.5" />
                  </a>
                ) : (
                  <Button
                    size="sm"
                    variant="ghost"
                    title="从远端取回到本地"
                    disabled={retrieve.isPending || !configured}
                    onClick={() => retrieve.mutate(entry.id)}
                  >
                    <ArrowDownToLine className="size-3.5" />
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  title="删除远端归档（先删远端，成功后才标记已删除）"
                  disabled={remove.isPending || entry.status === "已删除"}
                  onClick={() => setPendingDelete(entry)}
                >
                  <Trash2 className="size-3.5 text-danger" />
                </Button>
              </span>
            </li>
          ))}
        </ul>
      )}

      {data ? <Pager total={data.total} limit={PAGE} offset={offset} onChange={setOffset} busy={entries.isFetching} /> : null}

      <Dialog
        open={detailId !== null}
        onOpenChange={(open) => {
          if (!open) setDetailId(null);
        }}
      >
        <DialogContent className="max-h-[88vh] w-[calc(100%-1rem)] overflow-y-auto sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>归档详情 #{detailId ?? ""}</DialogTitle>
            <DialogDescription>远端路径、校验值与本地副本状态</DialogDescription>
          </DialogHeader>
          {detail.isLoading ? (
            <LoadingBlock label="正在读取归档详情…" />
          ) : detail.isError ? (
            <ErrorBlock message={isApiError(detail.error) ? detail.error.message : "无法读取归档详情"} retryable onRetry={() => void detail.refetch()} />
          ) : detail.data ? (
            <div className="space-y-2">
              {detail.data.entry.local_available && detail.data.entry.frame ? (
                <img
                  src={api.frameUrl(detail.data.entry.device_uid, detail.data.entry.frame.id, "full")}
                  alt="归档画面"
                  className="max-h-64 w-full rounded-md bg-black object-contain"
                />
              ) : null}
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[0.7rem]">
                <dt className="text-faint">设备</dt>
                <dd className="break-all text-soft">{detail.data.entry.device_name || detail.data.entry.device_uid || detail.data.entry.device_id}</dd>
                <dt className="text-faint">日期</dt>
                <dd className="text-soft">{detail.data.entry.day}</dd>
                <dt className="text-faint">远端路径</dt>
                <dd className="break-all font-mono text-[0.64rem] text-soft">{detail.data.entry.remote_path}</dd>
                <dt className="text-faint">sha256</dt>
                <dd className="break-all font-mono text-[0.64rem] text-soft">{detail.data.entry.sha256 || "—"}</dd>
                <dt className="text-faint">大小</dt>
                <dd className="text-soft">{formatBytes(detail.data.entry.size_bytes)}</dd>
                <dt className="text-faint">状态</dt>
                <dd className="text-soft">{detail.data.entry.status} · 尝试 {detail.data.entry.attempts} 次</dd>
                <dt className="text-faint">创建</dt>
                <dd className="text-soft">{formatDateTime(detail.data.entry.created_at_ms)}</dd>
                <dt className="text-faint">上传</dt>
                <dd className="text-soft">{formatDateTime(detail.data.entry.uploaded_at_ms ?? 0)}</dd>
                <dt className="text-faint">本地副本</dt>
                <dd className="text-soft">{detail.data.entry.local_available ? "在" : "仅远端"}</dd>
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