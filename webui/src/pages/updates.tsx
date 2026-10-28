import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ChevronRight, Download, PackagePlus, RefreshCw, Trash2, Upload, X } from "lucide-react";

import { Banner, EmptyBlock, ErrorBlock, LoadingBlock } from "@/components/async-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { MetaChip, PageHeader } from "@/components/page-header";
import { Panel } from "@/components/panel";
import { Button, buttonVariants } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
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
      // 0.3.7 审计 O06：原先用"不等于 latest"判定，把更高版本也说成"低于最新"。
      // 服务端的 latest 是最高 semver，所以这里必须真的比较大小（与 server 侧
      // `is_newer`/`parse_version` 同一套语义，只比较 major.minor.patch）。
      const latest = query.data?.latest_agent_version ?? "";
      const compare = compareSemver(pkg.version, latest);
      const hint =
        !latest || compare === 0
          ? ""
          : compare > 0
            ? "（高于当前最新版本，已成为最新）"
            : "（低于当前最新版本，不会成为最新）";
      setMessage(`已上传 v${pkg.version} · sha256 ${pkg.sha256.slice(0, 16)}…${hint}`);
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

      {/* The upload target lives in one panel, without a second card. */}
      <div
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          if (uploading) return;
          const file = event.dataTransfer.files?.[0];
          if (file) void upload(file);
        }}
      >
        <Panel title="版本分发" hint="拖入 .zip 安装包，或使用右侧按钮选择文件。">
          <div className="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
            <div className="min-w-0">
              <p className="text-xs font-medium tracking-[0.08em] text-muted-foreground uppercase">
                当前最新版本
              </p>
              <p className="tnum mt-1.5 break-all text-3xl leading-none font-bold tracking-[-0.03em] text-foreground">
                {latest ? `v${latest}` : "—"}
              </p>
              <p className="mt-2 max-w-md text-xs leading-relaxed text-muted-foreground">
                “最新”取已上传安装包里
                <b className="mx-0.5 font-medium text-foreground">语义版本最高</b>
                的一个；上传旧版本不会降低推送目标。
              </p>
            </div>

            <div className="flex min-w-0 flex-wrap items-center sm:pb-0.5">
              <Button
                className="w-full sm:w-auto"
                disabled={uploading}
                onClick={() => inputRef.current?.click()}
                title="选择 .zip 安装包"
              >
                {uploading ? <Spinner className="size-4" /> : <Upload className="size-4" aria-hidden />}
                选择安装包
              </Button>
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
            <div className="mt-3 min-w-0 border-t border-border pt-3">
              <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
                <span className="min-w-0 truncate font-mono text-xs text-muted-foreground">
                  {progress.name} · {formatBytes(progress.loaded)} / {formatBytes(progress.total)}
                </span>
                <span className="tnum shrink-0 text-xs font-medium text-foreground">{uploadPercent}%</span>
              </div>
              <Progress className="mt-2" value={uploadPercent} />
            </div>
          ) : null}
        </Panel>
      </div>

      <Panel
        title="安装包"
        hint="最新版本会自动推送给低于它的受控端"
        flush
        actions={<span className="tnum text-xs text-faint">{packages.length} 个</span>}
      >
        {query.isLoading ? (
          <div className="px-6 py-4">
            <LoadingBlock label="正在读取更新状态…" rows={4} />
          </div>
        ) : packages.length === 0 ? (
          <div className="px-6 py-4">
            <EmptyBlock
              title="还没有上传安装包"
              description="上传 observatory-agent-<版本>.zip 后，版本更高的受控端会自动收到升级指令。"
              action={
                <Button size="sm" onClick={() => inputRef.current?.click()}>
                  <PackagePlus className="size-3.5" aria-hidden />
                  上传第一个安装包
                </Button>
              }
            />
          </div>
        ) : (
          <ul className="min-w-0 divide-y divide-border">
            {packages.map((pkg) => {
              const isLatest = pkg.version === latest;
              return (
                <li
                  key={pkg.version}
                  className="grid min-w-0 grid-cols-1 gap-2.5 px-6 py-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:gap-3"
                >
                  <div className="grid min-w-0 gap-1.5">
                    <div className="flex min-w-0 flex-wrap items-center">
                      <span className="tnum mr-2 text-sm font-semibold text-foreground">v{pkg.version}</span>
                      {isLatest ? (
                        <MetaChip tone="ok" className="shrink-0">
                          当前最新
                        </MetaChip>
                      ) : (
                        <MetaChip className="shrink-0">历史版本</MetaChip>
                      )}
                    </div>
                    {pkg.notes ? (
                      <p className="min-w-0 break-words text-xs leading-relaxed text-muted-foreground">{pkg.notes}</p>
                    ) : null}
                    <p
                      className="min-w-0 truncate font-mono text-xs text-faint"
                      title={`${pkg.filename}\n大小 ${formatBytes(pkg.size_bytes)}\nsha256 ${pkg.sha256}`}
                    >
                      {pkg.filename} · {formatBytes(pkg.size_bytes)} · sha256 {pkg.sha256.slice(0, 16)}…
                    </p>
                    <p className="min-w-0 truncate text-xs text-faint">
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
                      className={cn("hover:text-destructive", tapClass)}
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
      </Panel>

      {data && latest ? (
        <Panel
          title={`仍低于最新版本的设备（${outdatedCount}）`}
          hint="低于 0.3.0 的受控端不在自动升级路径上，需要重新运行安装脚本覆盖安装。"
          flush
        >
          {outdatedCount === 0 ? (
            <p className="px-6 py-4 text-xs text-muted-foreground">
              所有受控端的版本都不低于当前最新版本 v{latest}。
            </p>
          ) : (
            <ul className="min-w-0 divide-y divide-border">
              {outdated.map((device) => (
                <li
                  key={device.uid}
                  className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2 px-6 py-3"
                >
                  <Link
                    to={`/device/${device.uid}/overview`}
                    className="tap-row flex min-w-0 items-center text-sm font-medium text-foreground no-underline hover:underline"
                  >
                    <span className="min-w-0 truncate">{device.name || device.uid}</span>
                    <ChevronRight className="ml-1 size-3.5 shrink-0 text-faint" aria-hidden />
                  </Link>
                  <span className="tnum shrink-0 font-mono text-xs text-faint">
                    v{device.version} → v{latest}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
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

/**
 * 比较两个 `major.minor.patch` 版本号，返回 -1 / 0 / 1。
 *
 * 0.3.7 审计 O06：上传后的提示原先用"是否等于 latest"来判断，于是上传更高版本
 * 也会被说成"低于当前最新版本"。服务端 `PackageService.latest` 取的是最高
 * semver（等价于 server 侧 `parse_version` 的元组比较），前端要给出正确提示就
 * 必须真的比大小。无法解析的版本号按 0.0.0 处理，不抛错（提示文案不值得崩页面）。
 */
function compareSemver(candidate: string, current: string): number {
  const parse = (value: string): [number, number, number] => {
    const matched = /^(\d+)\.(\d+)\.(\d+)$/.exec((value || "").trim());
    if (!matched) return [0, 0, 0];
    return [Number(matched[1]), Number(matched[2]), Number(matched[3])];
  };
  const left = parse(candidate);
  const right = parse(current);
  for (let index = 0; index < 3; index += 1) {
    if (left[index] !== right[index]) return left[index] > right[index] ? 1 : -1;
  }
  return 0;
}