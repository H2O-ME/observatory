import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ChevronRight, Download, PackagePlus, RefreshCw, Trash2, Upload, X } from "lucide-react";

import { GlowCard, GridPattern, ShimmerButton } from "@/components/aceternity";
import { Banner, EmptyBlock, ErrorBlock, LoadingBlock } from "@/components/async-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { MetaChip, PageHeader } from "@/components/page-header";
import { Section } from "@/components/section";
import { Button, buttonVariants } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { useViewport } from "@/hooks/use-viewport";
import { api, isApiError } from "@/lib/api";
import { formatBytes, formatDateTime } from "@/lib/format";
import type { Package as PackageDto } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Agent packages.
 *
 * "Latest" is the highest semver among uploaded packages (`PackageService.latest`
 * sorts by version), not "the last file uploaded": uploading 0.2.9 while 0.3.0
 * exists does not downgrade the offer. The hero states that rule instead of
 * inventing a "set as latest" button the server does not have.
 */
export function UpdatesPage() {
  const viewport = useViewport();
  const watch = viewport === "watch";
  const touch = viewport !== "desktop";
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const [progress, setProgress] = useState<{ loaded: number; total: number; name: string } | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [pendingDelete, setPendingDelete] = useState<PackageDto | null>(null);

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
  const latest = data?.latest_agent_version ?? null;
  const outdated = data?.outdated ?? [];
  const outdatedCount = outdated.length;
  const busy = query.isFetching;
  const uploading = progress !== null;
  const uploadPercent = progress && progress.total > 0 ? Math.round((progress.loaded / progress.total) * 100) : 0;
  const tapClass = touch ? "min-h-11 min-w-11" : undefined;

  return (
    <div className={cn("w-full max-w-full min-w-0 space-y-5", watch && "space-y-3")}>
      <PageHeader
        title="版本与更新"
        description={data ? `服务端 v${data.server_version} · 每 30 秒刷新` : "正在读取更新状态…"}
        actions={
          <Button
            size="sm"
            variant="outline"
            className={cn("w-full sm:w-auto", touch && "min-h-11")}
            onClick={() => void query.refetch()}
            disabled={busy}
          >
            <RefreshCw className={cn("size-3.5", busy && "animate-spin")} aria-hidden />
            刷新
          </Button>
        }
      />

      <input
        ref={inputRef}
        type="file"
        accept=".zip,application/zip"
        className="hidden"
        aria-label="选择受控端安装包"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void upload(file);
        }}
      />

      {message ? <Banner tone="ok">{message}</Banner> : null}
      {error ? <Banner tone="error">{error}</Banner> : null}

      {query.isError ? (
        <ErrorBlock
          message={isApiError(query.error) ? query.error.message : "无法读取更新状态"}
          retryable
          onRetry={() => void query.refetch()}
        />
      ) : null}

      {/* The hero keeps the upload target (drop or button) without a second card. */}
      <div
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          if (uploading) return;
          const file = event.dataTransfer.files?.[0];
          if (file) void upload(file);
        }}
      >
        <GlowCard className="min-w-0" contentClassName="relative p-4 sm:p-5">
          <GridPattern
            className="pointer-events-none absolute inset-full opacity-20"
            fade="radial"
            stroke="var(--c-grid-line)"
            strokeDasharray="4 8"
          />
          <div className="relative grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-[minmax(0,auto)_auto] sm:items-end sm:gap-8">
            <div className="min-w-0">
              <p className="tnum text-gradient break-all text-[1.65rem] font-semibold leading-tight tracking-tight sm:text-[1.85rem]">
                {latest ? `v${latest}` : "—"}
              </p>
              <p className="mt-2 max-w-md text-[0.8rem] leading-relaxed text-soft">
                “最新”取已上传安装包里
                <b className="mx-0.5 font-semibold text-ink">语义版本最高</b>
                的一个；上传旧版本不会降低推送目标。
              </p>
            </div>

            <div className="flex min-w-0 flex-wrap items-center sm:pb-1">
              <ShimmerButton
                className="w-full sm:w-auto"
                busy={uploading}
                disabled={uploading}
                onClick={() => inputRef.current?.click()}
                title="选择 .zip 安装包"
              >
                <span className="inline-flex items-center">
                  <Upload className="mr-1.5 size-4" aria-hidden />
                  选择安装包
                </span>
              </ShimmerButton>
              {uploading ? (
                <Button
                  size="sm"
                  variant="ghost"
                  className={cn("mt-2 w-full sm:ml-2 sm:mt-0 sm:w-auto", touch && "min-h-11")}
                  onClick={() => abortRef.current?.abort()}
                >
                  <X className="size-4" aria-hidden />
                  取消
                </Button>
              ) : null}
            </div>
          </div>

          {progress ? (
            <div className="relative mt-3 min-w-0 rounded-xl border border-line bg-sunken p-3">
              <div className="flex min-w-0 flex-wrap items-center justify-between">
                <span className="min-w-0 truncate font-mono text-[0.75rem] text-soft">
                  {progress.name} · {formatBytes(progress.loaded)} / {formatBytes(progress.total)}
                </span>
                <span className="tnum shrink-0 text-[0.78rem] font-semibold text-brand">{uploadPercent}%</span>
              </div>
              <Progress className="mt-2" value={uploadPercent} />
            </div>
          ) : null}
        </GlowCard>
      </div>

      <Section title="安装包">
        {query.isLoading ? (
          <LoadingBlock label="正在读取更新状态…" rows={4} />
        ) : packages.length === 0 ? (
          <EmptyBlock
            title="还没有上传安装包"
            description="上传 observatory-agent-<版本>.zip 后，版本更高的受控端会自动收到升级指令。"
            action={
              <Button onClick={() => inputRef.current?.click()}>
                <PackagePlus className="size-3.5" aria-hidden />
                上传第一个安装包
              </Button>
            }
          />
        ) : (
          <ul className="grid min-w-0 gap-3">
            {packages.map((pkg) => {
              const isLatest = pkg.version === latest;
              return (
                <li
                  key={pkg.version}
                  className={cn(
                    "grid min-w-0 grid-cols-1 gap-2.5 rounded-xl border px-3.5 py-3.5 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:gap-3",
                    isLatest ? "border-line bg-brand-soft" : "border-line bg-sunken",
                  )}
                >
                  <div className="grid min-w-0 gap-1.5">
                    <div className="flex min-w-0 flex-wrap items-center">
                      <span className="tnum mr-2 text-[0.95rem] font-semibold text-ink">v{pkg.version}</span>
                      {isLatest ? (
                        <MetaChip tone="ok" className="shrink-0">
                          当前最新
                        </MetaChip>
                      ) : null}
                    </div>
                    {pkg.notes ? (
                      <p className="min-w-0 break-words text-[0.78rem] leading-relaxed text-soft">{pkg.notes}</p>
                    ) : null}
                    <p
                      className="min-w-0 truncate font-mono text-[0.72rem] text-faint"
                      title={`${pkg.filename}\n大小 ${formatBytes(pkg.size_bytes)}\nsha256 ${pkg.sha256}`}
                    >
                      {pkg.filename} · {formatBytes(pkg.size_bytes)} · sha256 {pkg.sha256.slice(0, 16)}…
                    </p>
                    <p className="min-w-0 truncate text-[0.72rem] text-faint">
                      上传于 {formatDateTime(pkg.created_at_ms)}
                    </p>
                  </div>

                  <div className="flex min-w-0 flex-wrap items-center justify-start sm:justify-end">
                    <a
                      href={pkg.download_url}
                      className={cn(buttonVariants({ variant: "ghost", size: "sm" }), "no-underline", tapClass)}
                      title="下载该版本安装包"
                      aria-label={`下载 v${pkg.version} 安装包`}
                    >
                      <Download className="size-4" aria-hidden />
                      <span className="ml-1.5 hidden sm:inline">下载</span>
                    </a>
                    <Button
                      size="sm"
                      variant="ghost"
                      className={cn("text-soft hover:text-danger", tapClass)}
                      disabled={remove.isPending}
                      onClick={() => setPendingDelete(pkg)}
                      title={isLatest ? "删除当前最新版本会回退到次高版本" : "删除该安装包"}
                      aria-label={`删除 v${pkg.version} 安装包`}
                    >
                      <Trash2 className="size-4" aria-hidden />
                      <span className="ml-1.5 hidden sm:inline">删除</span>
                    </Button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </Section>

      {data && latest ? (
        <Section
          title={`仍低于最新版本的设备（${outdatedCount}）`}
          hint="低于 0.3.0 的受控端不在自动升级路径上，需要重新运行安装脚本覆盖安装。"
        >
          {outdatedCount === 0 ? (
            <p className="text-[0.8rem] text-soft">所有受控端的版本都不低于当前最新版本 v{latest}。</p>
          ) : (
            <ul className="grid min-w-0 gap-2">
              {outdated.map((device) => (
                <li
                  key={device.uid}
                  className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2 rounded-xl border border-line bg-sunken px-3 py-2"
                >
                  <Link
                    to={`/device/${device.uid}/overview`}
                    className="tap-row flex min-w-0 items-center text-[0.82rem] font-medium text-brand no-underline"
                  >
                    <span className="min-w-0 truncate">{device.name || device.uid}</span>
                    <ChevronRight className="ml-1 size-3.5 shrink-0 opacity-70" aria-hidden />
                  </Link>
                  <span className="tnum shrink-0 font-mono text-[0.72rem] text-faint">
                    v{device.version} → v{latest}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Section>
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
