import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Download, PackagePlus, RefreshCw, Trash2, Upload, X } from "lucide-react";

import { Banner, EmptyBlock, ErrorBlock, LoadingBlock, PageHeader } from "@/components/async-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { Button, buttonVariants } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { api, isApiError } from "@/lib/api";
import { formatBytes, formatDateTime } from "@/lib/format";
import type { Package } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Agent packages.
 *
 * "Latest" is the highest semver among uploaded packages (`PackageService.latest`
 * sorts by version), not "the last file uploaded": uploading 0.2.9 while 0.3.0
 * exists does not downgrade the offer. The page states that rule instead of
 * inventing a "set as latest" button the server does not have.
 */
export function UpdatesPage() {
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const [progress, setProgress] = useState<{ loaded: number; total: number; name: string } | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [pendingDelete, setPendingDelete] = useState<Package | null>(null);

  const query = useQuery({ queryKey: ["updates"], queryFn: api.getUpdates, refetchInterval: 30_000 });

  const remove = useMutation({
    mutationFn: (version: string) => api.deletePackage(version),
    onSuccess: (result) => {
      setMessage(result.message);
      setError("");
      void queryClient.invalidateQueries({ queryKey: ["updates"] });
      void queryClient.invalidateQueries({ queryKey: ["devices"] });
    },
    onError: (failure) => setError(isApiError(failure) ? failure.message : "删除失败"),
  });

  const upload = async (file: File) => {
    const controller = new AbortController();
    abortRef.current = controller;
    setError("");
    setMessage("");
    setProgress({ loaded: 0, total: file.size, name: file.name });
    try {
      const pkg = await api.uploadPackage(file, "", {
        signal: controller.signal,
        onProgress: (loaded, total) => setProgress({ loaded, total: total || file.size, name: file.name }),
      });
      setMessage(
        `已上传 v${pkg.version} · sha256 ${pkg.sha256.slice(0, 16)}…${
          query.data?.latest_agent_version && pkg.version === query.data.latest_agent_version ? "" : "（低于当前最新版本，不会成为最新）"
        }`,
      );
      void queryClient.invalidateQueries({ queryKey: ["updates"] });
      void queryClient.invalidateQueries({ queryKey: ["devices"] });
    } catch (failure) {
      if (isApiError(failure) && failure.message.includes("取消")) setMessage("已取消上传");
      else setError(isApiError(failure) ? failure.message : "上传失败");
    } finally {
      abortRef.current = null;
      setProgress(null);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  const data = query.data;
  const packages = data?.packages ?? [];

  return (
    <div className="space-y-3">
      <PageHeader
        title="版本与更新"
        subtitle={`服务端 v${data?.server_version ?? "—"} · 最新受控端版本 ${
          data?.latest_agent_version ? `v${data.latest_agent_version}` : "（还没有安装包）"
        }`}
        actions={
          <>
            <Button size="sm" variant="outline" onClick={() => void query.refetch()} disabled={query.isFetching}>
              <RefreshCw className={cn("size-3.5", query.isFetching && "animate-spin")} />
              刷新
            </Button>
            <Button size="sm" onClick={() => inputRef.current?.click()} disabled={progress !== null}>
              <Upload className="size-3.5" />
              上传安装包
            </Button>
            <input
              ref={inputRef}
              type="file"
              accept=".zip,application/zip"
              className="hidden"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void upload(file);
              }}
            />
          </>
        }
      />

      {message ? <Banner tone="ok">{message}</Banner> : null}
      {error ? <Banner tone="error">{error}</Banner> : null}

      {progress ? (
        <div className="rounded-panel border border-line bg-panel p-2.5">
          <div className="mb-1 flex items-center justify-between gap-2 text-[0.72rem] text-soft">
            <span className="truncate">
              上传 {progress.name} · {formatBytes(progress.loaded)} / {formatBytes(progress.total)}
            </span>
            <Button size="sm" variant="ghost" onClick={() => abortRef.current?.abort()}>
              <X className="size-3.5" />
              取消
            </Button>
          </div>
          <Progress value={progress.total > 0 ? Math.round((progress.loaded / progress.total) * 100) : 0} />
        </div>
      ) : null}

      <Banner tone="info">
        “最新”是已上传安装包里<b className="mx-0.5 font-medium">语义版本最高</b>的那个；上传更高版本才会改变推送目标，
        上传旧版本不会把在线设备降到旧版。安装包必须包含 sha256，否则服务器拒绝向受控端推送。
      </Banner>

      {query.isError ? (
        <ErrorBlock
          message={isApiError(query.error) ? query.error.message : "无法读取更新状态"}
          retryable
          onRetry={() => void query.refetch()}
        />
      ) : query.isLoading ? (
        <LoadingBlock label="正在读取更新状态…" rows={4} />
      ) : packages.length === 0 ? (
        <EmptyBlock
          title="还没有上传安装包"
          description="上传 observatory-agent-<版本>.zip 后，版本高于受控端当前版本的设备会自动收到升级指令。"
          action={
            <Button size="sm" onClick={() => inputRef.current?.click()}>
              <PackagePlus className="size-3.5" />
              上传第一个安装包
            </Button>
          }
        />
      ) : (
        <ul className="divide-y divide-line rounded-panel border border-line bg-panel">
          {packages.map((pkg) => {
            const latest = pkg.version === data?.latest_agent_version;
            return (
              <li key={pkg.version} className="flex flex-wrap items-start gap-2 px-2.5 py-2">
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="font-mono text-[0.82rem] font-medium text-ink">v{pkg.version}</span>
                    {latest ? (
                      <span className="rounded border border-ok/30 bg-ok/10 px-1.5 py-0.5 text-[0.62rem] text-ok">
                        当前最新
                      </span>
                    ) : null}
                  </span>
                  <span className="mt-0.5 block truncate text-[0.66rem] text-faint">
                    {pkg.filename} · {formatBytes(pkg.size_bytes)} · sha256 {pkg.sha256.slice(0, 16)}… ·{" "}
                    {formatDateTime(pkg.created_at_ms)}
                  </span>
                  {pkg.notes ? <span className="mt-0.5 block text-[0.68rem] text-soft">{pkg.notes}</span> : null}
                </span>
                <span className="flex shrink-0 items-center gap-1">
                  <a
                    href={pkg.download_url}
                    className={cn(buttonVariants({ variant: "ghost", size: "sm" }), "no-underline")}
                    title="下载该版本安装包"
                  >
                    <Download className="size-3.5" />
                    下载
                  </a>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={remove.isPending}
                    onClick={() => setPendingDelete(pkg)}
                    title={latest ? "删除当前最新版本会回退到次高版本" : "删除该安装包"}
                  >
                    <Trash2 className="size-3.5 text-danger" />
                    删除
                  </Button>
                </span>
              </li>
            );
          })}
        </ul>
      )}

      {data && data.outdated.length > 0 ? (
        <section className="rounded-panel border border-line bg-panel p-3">
          <h2 className="mb-2 text-xs font-semibold text-soft">
            仍低于最新版本的设备（{data.outdated.length}）
          </h2>
          <ul className="space-y-1 text-[0.72rem]">
            {data.outdated.map((device) => (
              <li key={device.uid} className="flex items-center justify-between gap-2">
                <Link to={`/device/${device.uid}/overview`} className="truncate text-brand no-underline">
                  {device.name || device.uid}
                </Link>
                <span className="shrink-0 font-mono text-faint">
                  v{device.version}
                  {data.latest_agent_version ? ` → v${data.latest_agent_version}` : ""}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[0.66rem] leading-relaxed text-faint">
            低于 0.3.0 的受控端不在自动升级路径上（版本下限），需要重新运行安装脚本覆盖安装；这类设备在设备详情页会显示“无法自动升级”。
          </p>
        </section>
      ) : null}

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
        title="删除安装包"
        description={
          pendingDelete
            ? `确定删除 v${pendingDelete.version} 的安装包文件？删除后无法再向设备提供该版本下载。`
            : ""
        }
        confirmLabel="删除"
        busy={remove.isPending}
        onConfirm={() => {
          if (!pendingDelete) return;
          remove.mutate(pendingDelete.version, { onSettled: () => setPendingDelete(null) });
        }}
      />
    </div>
  );
}