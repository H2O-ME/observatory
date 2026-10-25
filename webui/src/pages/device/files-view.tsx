import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUp,
  ChevronRight,
  Copy,
  Download,
  File as FileIcon,
  FolderClosed,
  FolderInput,
  FolderPlus,
  HardDrive,
  MoreHorizontal,
  Pencil,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";

import { BentoGrid, BentoGridItem, GlowCard, ShimmerButton } from "@/components/aceternity";
import { Banner, EmptyBlock, ErrorBlock, LoadingBlock } from "@/components/async-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { Meter } from "@/components/meter";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { useViewport } from "@/hooks/use-viewport";
import { api, isApiError } from "@/lib/api";
import { dirname, formatBytes, formatDateTime, joinPath } from "@/lib/format";
import { capabilitiesOf } from "@/lib/presence";
import type { Device, FileEntry } from "@/lib/types";
import { cn } from "@/lib/utils";

/** One agent chunk (`MAX_CHUNK_BYTES` on both sides). */
const CHUNK_BYTES = 1024 * 1024;

interface Entry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
  /** Time reported by the controlled machine — never the server clock. */
  deviceTimeMs: number;
  hidden: boolean;
}

interface Drive {
  name: string;
  path: string;
  free: number | null;
  total: number | null;
  filesystem: string;
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Console `FileEntry` (`console/dto.py::file_entry_out`). */
function entryFromDto(entry: FileEntry): Entry {
  return {
    name: entry.name,
    path: entry.path,
    is_dir: entry.is_dir,
    size: entry.size,
    deviceTimeMs: entry.device_time_ms,
    hidden: entry.hidden,
  };
}

/**
 * Search/stat results are the *agent's* raw payload (the console passes them
 * through), so the timestamp is still named `mtime_ms` there. Both names are
 * device-reported; neither is the server clock.
 */
function entryFromAgent(raw: Record<string, unknown>): Entry | null {
  if (typeof raw.name !== "string" || typeof raw.path !== "string") return null;
  const time = numberOrNull(raw.device_time_ms) ?? numberOrNull(raw.mtime_ms) ?? 0;
  return {
    name: raw.name,
    path: raw.path,
    is_dir: Boolean(raw.is_dir),
    size: Number(raw.size ?? 0) || 0,
    deviceTimeMs: time,
    hidden: Boolean(raw.hidden),
  };
}

function driveFromDto(entry: FileEntry): Drive {
  return {
    name: entry.name || entry.path,
    path: entry.path,
    // `free`/`filesystem` are part of the 0.3.0 contract; if a server omits them
    // the UI shows "—" instead of claiming "0 B" (the 0.2.x bug).
    free: numberOrNull(entry.free),
    total: numberOrNull(entry.size),
    filesystem: typeof entry.filesystem === "string" ? entry.filesystem : "",
  };
}

function crumbs(path: string): Array<{ label: string; path: string }> {
  const items = [{ label: "驱动器", path: "" }];
  if (!path) return items;
  const separator = path.includes("/") ? "/" : "\\";
  const parts = path.split(/[\\/]/).filter(Boolean);
  let acc = "";
  for (const part of parts) {
    if (!acc) {
      acc = /^[A-Za-z]:$/.test(part) ? `${part}${separator}` : part;
    } else {
      acc = `${acc.replace(/[\\/]+$/, "")}${separator}${part}`;
    }
    items.push({ label: part, path: acc });
  }
  return items;
}

type ActionKind = "mkdir" | "rename" | "copy" | "move" | "search";

interface ActionState {
  kind: ActionKind;
  entry?: Entry;
}

/**
 * Files manager.
 *
 * Every control is gated by the server's `capabilities.files`; when it is false
 * the page states the server's own reason and renders no mutating controls
 * (A13). Chunked transfers reuse the single client, so they inherit the server
 * deadline budget, cancellation and the 401 handler instead of the 0.2.x fixed
 * 20 s timeout.
 */
export function FilesView({ device }: { device: Device }) {
  const queryClient = useQueryClient();
  const viewport = useViewport();
  const watch = viewport === "watch";
  const capabilities = capabilitiesOf(device.presence);
  const uid = device.uid;

  const [path, setPath] = useState("");
  const [action, setAction] = useState<ActionState | null>(null);
  const [actionText, setActionText] = useState("");
  const [recursive, setRecursive] = useState(true);
  const [pendingDelete, setPendingDelete] = useState<Entry | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string>("");
  const [progress, setProgress] = useState<{ label: string; loaded: number; total: number } | null>(null);
  const [search, setSearch] = useState<{ entries: Entry[]; truncated: boolean } | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const uploadRef = useRef<HTMLInputElement | null>(null);

  const listQuery = useQuery({
    queryKey: ["files", uid, path],
    queryFn: () => api.listFiles(uid, path),
    enabled: capabilities.files,
  });

  useEffect(() => {
    // Leaving the tab (unmount) cancels an in-flight transfer instead of letting
    // it keep the socket busy with nobody watching.
    return () => abortRef.current?.abort();
  }, []);

  const drives: Drive[] = useMemo(
    () => (listQuery.data?.drives ?? []).map(driveFromDto),
    [listQuery.data?.drives],
  );
  const entries: Entry[] = useMemo(
    () => (listQuery.data?.entries ?? []).map(entryFromDto),
    [listQuery.data?.entries],
  );
  const parent = dirname(path);

  const refresh = async () => {
    await listQuery.refetch();
    void queryClient.invalidateQueries({ queryKey: ["device", uid] });
  };

  const runOp = async (body: Parameters<typeof api.fileOp>[1], label: string) => {
    setBusy(label);
    setError("");
    setMessage("");
    try {
      const result = await api.fileOp(uid, body);
      if (!result.ok) throw new Error(result.error || "受控端返回失败");
      setMessage(`${label}完成（受控端耗时 ${result.elapsed_ms} ms）`);
      await refresh();
      return result;
    } catch (failure) {
      setError(isApiError(failure) ? failure.message : failure instanceof Error ? failure.message : `${label}失败`);
      return null;
    } finally {
      setBusy("");
    }
  };

  const openAction = (kind: ActionKind, entry?: Entry) => {
    setAction({ kind, entry });
    setActionText(kind === "search" ? "*.log" : entry ? entry.name : "");
    setError("");
    setMessage("");
  };

  const submitAction = async () => {
    if (!action) return;
    const value = actionText.trim();
    if (!value) {
      setError("请输入内容");
      return;
    }
    if (action.kind === "mkdir") {
      const done = await runOp({ op: "mkdir", path: joinPath(path, value) }, "新建文件夹");
      if (done) setAction(null);
      return;
    }
    if (action.kind === "search") {
      setBusy("搜索");
      setError("");
      setSearch(null);
      try {
        const result = await api.fileOp(uid, { op: "search", path, pattern: value, recursive });
        const raw = Array.isArray(result.data.entries) ? (result.data.entries as Record<string, unknown>[]) : [];
        setSearch({
          entries: raw.map(entryFromAgent).filter((item): item is Entry => item !== null),
          truncated: Boolean(result.data.truncated),
        });
        setAction(null);
      } catch (failure) {
        setError(isApiError(failure) ? failure.message : "搜索失败");
      } finally {
        setBusy("");
      }
      return;
    }
    const entry = action.entry;
    if (!entry) return;
    let body: Parameters<typeof api.fileOp>[1];
    if (action.kind === "rename") {
      const target = joinPath(dirname(entry.path) ?? path, value);
      body = { op: "rename", path: entry.path, target };
    } else {
      body = { op: action.kind, path: entry.path, target: value };
    }
    const label = action.kind === "rename" ? "重命名" : action.kind === "copy" ? "复制" : "移动";
    const done = await runOp(body, label);
    if (done) setAction(null);
  };

  const deleteEntry = async () => {
    if (!pendingDelete) return;
    const entry = pendingDelete;
    // F43: 关闭对话框推迟到操作结束——递归删除大目录可能数秒，立即关闭时
    // 没有 busy 反馈且窗口期内可双击触发两次 DELETE。
    try {
      await runOp({ op: "delete", path: entry.path, recursive: entry.is_dir }, "删除");
    } finally {
      setPendingDelete(null);
    }
  };

  const upload = async (file: File) => {
    if (!capabilities.files) return;
    if (file.size === 0) {
      setError("空文件无法上传：服务端拒绝 0 字节分片");
      return;
    }
    const target = joinPath(path, file.name);
    const controller = new AbortController();
    abortRef.current = controller;
    setBusy("上传");
    setError("");
    setMessage("");
    setProgress({ label: `上传 ${file.name}`, loaded: 0, total: file.size });
    try {
      let offset = 0;
      while (offset < file.size) {
        const chunk = file.slice(offset, Math.min(offset + CHUNK_BYTES, file.size));
        const result = await api.writeChunk(uid, target, offset, chunk, offset === 0, controller.signal);
        if (!result.ok) throw new Error(result.error || "写入失败");
        offset += chunk.size;
        setProgress({ label: `上传 ${file.name}`, loaded: offset, total: file.size });
      }
      setMessage(`已上传 ${file.name}（${formatBytes(file.size)}）`);
      await refresh();
    } catch (failure) {
      if (isApiError(failure) && failure.code === "INTERNAL" && failure.message.includes("取消")) {
        setMessage("已取消上传");
      } else {
        setError(isApiError(failure) ? failure.message : failure instanceof Error ? failure.message : "上传失败");
      }
    } finally {
      abortRef.current = null;
      setProgress(null);
      setBusy("");
      if (uploadRef.current) uploadRef.current.value = "";
    }
  };

  const download = async (entry: Entry) => {
    const controller = new AbortController();
    abortRef.current = controller;
    setBusy("下载");
    setError("");
    setMessage("");
    setProgress({ label: `下载 ${entry.name}`, loaded: 0, total: entry.size });
    try {
      const parts: BlobPart[] = [];
      let offset = 0;
      let total = entry.size;
      for (let guard = 0; guard < 100_000; guard += 1) {
        const chunk = await api.readChunk(uid, entry.path, offset, CHUNK_BYTES, controller.signal);
        if (chunk.size > 0) total = chunk.size;
        if (chunk.blob.size === 0) break;
        parts.push(chunk.blob);
        offset += chunk.blob.size;
        setProgress({ label: `下载 ${entry.name}`, loaded: offset, total });
        if (chunk.eof) break;
      }
      const blob = new Blob(parts);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = entry.name;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
      setMessage(`已下载 ${entry.name}（${formatBytes(blob.size)}）`);
    } catch (failure) {
      if (isApiError(failure) && failure.message.includes("取消")) setMessage("已取消下载");
      else setError(isApiError(failure) ? failure.message : "下载失败");
    } finally {
      abortRef.current = null;
      setProgress(null);
      setBusy("");
    }
  };

  if (!capabilities.files) {
    return (
      <div className="w-full max-w-full min-w-0 space-y-3 overflow-x-hidden">
        <Banner tone="warn">
          文件管理当前不可用：{device.presence.reason || "设备没有活动链路"}
        </Banner>
        <EmptyBlock
          title="没有可浏览的文件"
          description="受控端恢复活动通道后本页会自动可用。"
        />
      </div>
    );
  }

  const listing = search ?? null;
  const crumbList = crumbs(path);
  const progressPercent =
    progress && progress.total > 0 ? Math.min(100, Math.round((progress.loaded / progress.total) * 100)) : 0;

  return (
    <div
      className={cn(
        "w-full max-w-full min-w-0 overflow-x-hidden",
        watch ? "space-y-3" : "space-y-4 sm:space-y-5",
      )}
    >
      {/* Toolbar: the only controls, one compact row per breakpoint. */}
      <div className="surface min-w-0 p-3 sm:p-4">
        <div className="grid grid-cols-3 gap-2 sm:grid-cols-5">
          <Button
            size="sm"
            variant="outline"
            className="tap w-full justify-start"
            onClick={() => void refresh()}
            disabled={listQuery.isFetching || Boolean(busy)}
          >
            <RefreshCw className={cn("mr-2 size-4", listQuery.isFetching && "animate-spin")} aria-hidden />
            刷新
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="tap w-full justify-start"
            onClick={() => setPath(parent ?? "")}
            disabled={!path}
          >
            <ArrowUp className="mr-2 size-4" aria-hidden />
            上级
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="tap w-full justify-start"
            onClick={() => openAction("mkdir")}
            disabled={Boolean(busy)}
          >
            <FolderPlus className="mr-2 size-4" aria-hidden />
            新建
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="tap w-full justify-start"
            onClick={() => openAction("search")}
            disabled={Boolean(busy)}
          >
            <Search className="mr-2 size-4" aria-hidden />
            搜索
          </Button>
          <ShimmerButton
            block
            type="button"
            title="上传文件"
            className="col-span-2 w-full sm:col-span-1"
            disabled={Boolean(busy) || !path}
            busy={busy === "上传"}
            onClick={() => uploadRef.current?.click()}
          >
            <Upload className="mr-2 size-4" aria-hidden />
            上传
          </ShimmerButton>
          <input
            ref={uploadRef}
            type="file"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void upload(file);
            }}
          />
        </div>

        {search ? (
          <Button
            size="sm"
            variant="ghost"
            className="tap mt-2 w-full justify-center"
            onClick={() => setSearch(null)}
          >
            退出搜索结果
          </Button>
        ) : null}
      </div>

      {/* In-flight transfer: progress + the only cancel affordance. */}
      {progress ? (
        <GlowCard contentClassName={watch ? "p-2.5" : "p-3 sm:p-4"}>
          <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2">
            <div className="min-w-0">
              <p className={cn("truncate font-medium text-ink", watch ? "text-[0.78rem]" : "text-[0.82rem]")}>
                {progress.label}
              </p>
              <p className="tnum mt-0.5 truncate text-[0.7rem] text-soft">
                {formatBytes(progress.loaded)} / {formatBytes(progress.total)} · {progressPercent}%
              </p>
            </div>
            <Button size="sm" variant="outline" className="tap" onClick={() => abortRef.current?.abort()}>
              <X className="mr-1.5 size-4" aria-hidden />
              取消
            </Button>
          </div>
          <Progress className="mt-2.5" value={progressPercent} />
        </GlowCard>
      ) : null}

      {message ? <Banner tone="ok">{message}</Banner> : null}
      {error ? <Banner tone="error">{error}</Banner> : null}

      {/* The only horizontally scrolling row on this page: the breadcrumb. */}
      <nav aria-label="当前路径" className="flex min-w-0 max-w-full items-center px-0.5">
        <FolderClosed className="mr-1 size-3.5 shrink-0 text-faint" aria-hidden />
        <div className="hide-scrollbar min-w-0 flex-1 overflow-x-auto">
          <ol className="flex min-w-max items-center">
            {crumbList.map((crumb, index) => {
              const last = index === crumbList.length - 1;
              return (
                <li key={`${crumb.path}-${index}`} className="flex shrink-0 items-center">
                  {index > 0 ? <ChevronRight className="mx-0.5 size-3.5 shrink-0 text-faint" aria-hidden /> : null}
                  <button
                    type="button"
                    aria-current={last ? "location" : undefined}
                    onClick={() => {
                      setSearch(null);
                      setPath(crumb.path);
                    }}
                    className={cn(
                      "tap inline-flex items-center rounded-lg px-2 text-[0.78rem] transition-colors hover:bg-sunken",
                      last ? "font-semibold text-ink" : "text-soft hover:text-ink",
                    )}
                  >
                    <span className="max-w-[10rem] truncate">{crumb.label}</span>
                  </button>
                </li>
              );
            })}
          </ol>
        </div>
      </nav>

      {listQuery.isError ? (
        <ErrorBlock
          message={isApiError(listQuery.error) ? listQuery.error.message : "无法读取目录"}
          retryable
          onRetry={() => void listQuery.refetch()}
        />
      ) : listQuery.isLoading ? (
        <LoadingBlock label="正在读取目录…" />
      ) : listing ? (
        <div className="space-y-2">
          <Banner tone="info">
            搜索 {search?.entries.length ?? 0} 项{search?.truncated ? "（结果被截断）" : ""}
          </Banner>
          {search && search.entries.length === 0 ? (
            <EmptyBlock title="没有匹配的文件" />
          ) : (
            <EntryList
              entries={search?.entries ?? []}
              onOpen={(entry) => {
                if (entry.is_dir) {
                  setSearch(null);
                  setPath(entry.path);
                }
              }}
              onRename={(entry) => openAction("rename", entry)}
              onCopy={(entry) => openAction("copy", entry)}
              onMove={(entry) => openAction("move", entry)}
              onDelete={setPendingDelete}
              onDownload={(entry) => void download(entry)}
              busy={Boolean(busy)}
            />
          )}
        </div>
      ) : !path ? (
        drives.length === 0 ? (
          <EmptyBlock title="没有可用的驱动器" description="受控端没有返回任何逻辑驱动器。" />
        ) : (
          <BentoGrid>
            {drives.map((drive) => {
              const percent =
                drive.free !== null && drive.total !== null && drive.total > 0
                  ? (drive.free / drive.total) * 100
                  : null;
              return (
                <BentoGridItem
                  key={drive.path}
                  icon={<HardDrive aria-hidden />}
                  title={<span className="block truncate">{drive.name}</span>}
                  description={`${drive.filesystem || "未知文件系统"} · 逻辑驱动器`}
                  footer={
                    <Button
                      variant="outline"
                      className="tap w-full justify-between"
                      onClick={() => setPath(drive.path)}
                    >
                      <span className="truncate">浏览此驱动器</span>
                      <ChevronRight className="ml-2 size-4 shrink-0" aria-hidden />
                    </Button>
                  }
                >
                  <div className="mt-2 min-w-0">
                    {percent === null ? (
                      <p className="text-[0.72rem] text-faint">剩余空间：受控端未上报</p>
                    ) : (
                      <Meter
                        compact
                        label="剩余空间"
                        value={percent}
                        hint={`${formatBytes(drive.free ?? 0)} 可用 / ${formatBytes(drive.total ?? 0)}`}
                      />
                    )}
                  </div>
                </BentoGridItem>
              );
            })}
          </BentoGrid>
        )
      ) : entries.length === 0 ? (
        <EmptyBlock title="这个目录是空的" description={path ? `路径：${path}` : undefined} />
      ) : (
        <EntryList
          entries={entries}
          onOpen={(entry) => {
            if (entry.is_dir) setPath(entry.path);
          }}
          onRename={(entry) => openAction("rename", entry)}
          onCopy={(entry) => openAction("copy", entry)}
          onMove={(entry) => openAction("move", entry)}
          onDelete={setPendingDelete}
          onDownload={(entry) => void download(entry)}
          busy={Boolean(busy)}
        />
      )}

      {listQuery.data?.truncated ? <Banner tone="warn">目录条目过多，受控端已截断返回。</Banner> : null}

      <p className="px-0.5 text-[0.7rem] leading-relaxed text-faint">
        分片 {CHUNK_BYTES / 1024} KiB · 单文件上传上限由服务端设置（默认 512 MiB）
      </p>

      <ActionDialog
        action={action}
        value={actionText}
        recursive={recursive}
        busy={Boolean(busy)}
        onChange={setActionText}
        onRecursiveChange={setRecursive}
        onCancel={() => setAction(null)}
        onSubmit={() => void submitAction()}
        basePath={path}
      />

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
        title="删除文件/文件夹"
        description={
          pendingDelete
            ? `确定删除「${pendingDelete.name}」？${pendingDelete.is_dir ? "文件夹将连同内容一起删除。" : ""}此操作不可撤销。`
            : ""
        }
        confirmLabel="删除"
        busy={busy === "删除"}
        onConfirm={() => void deleteEntry()}
      />
    </div>
  );
}

/** Small toolbar-sized icon button with a real label for accessibility. */
function IconAction({
  label,
  icon: Icon,
  disabled,
  danger = false,
  onClick,
}: {
  label: string;
  icon: typeof Download;
  disabled?: boolean;
  danger?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "tap inline-flex size-9 items-center justify-center rounded-lg text-soft transition-colors hover:bg-sunken hover:text-ink disabled:pointer-events-none disabled:opacity-40",
        danger && "text-danger hover:text-danger",
      )}
    >
      <Icon className="size-4" aria-hidden />
    </button>
  );
}

/**
 * One directory listing.
 *
 * Desktop keeps a compact table-like row with direct actions; phone/watch get a
 * full-width tappable card whose actions live in a bottom sheet (`.ui-dialog`
 * turns into one automatically), so no row squeezes five tiny icons.
 */
function EntryList({
  entries,
  onOpen,
  onRename,
  onCopy,
  onMove,
  onDelete,
  onDownload,
  busy,
}: {
  entries: Entry[];
  onOpen: (entry: Entry) => void;
  onRename: (entry: Entry) => void;
  onCopy: (entry: Entry) => void;
  onMove: (entry: Entry) => void;
  onDelete: (entry: Entry) => void;
  onDownload: (entry: Entry) => void;
  busy: boolean;
}) {
  const viewport = useViewport();
  const desktop = viewport === "desktop";
  const watch = viewport === "watch";
  const [sheetEntry, setSheetEntry] = useState<Entry | null>(null);

  return (
    <>
      {desktop ? (
        <div className="surface min-w-0 overflow-hidden">
          <ul className="min-w-0">
            {entries.map((entry) => (
              <li
                key={entry.path}
                className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 border-b border-line px-3 py-2 last:border-b-0 hover:bg-sunken"
              >
                <button
                  type="button"
                  title={entry.path}
                  onClick={() => (entry.is_dir ? onOpen(entry) : setSheetEntry(entry))}
                  className="tap-row flex min-w-0 items-center rounded-lg px-1 text-left"
                >
                  <span
                    className={cn(
                      "mr-2.5 flex size-8 shrink-0 items-center justify-center rounded-lg border border-line bg-sunken",
                      entry.is_dir ? "text-warn" : "text-faint",
                    )}
                  >
                    {entry.is_dir ? (
                      <FolderClosed className="size-4" aria-hidden />
                    ) : (
                      <FileIcon className="size-4" aria-hidden />
                    )}
                  </span>
                  <span className="min-w-0">
                    <span
                      className={cn(
                        "block truncate text-[0.84rem] font-medium",
                        entry.hidden ? "text-faint" : "text-ink",
                      )}
                    >
                      {entry.name}
                    </span>
                    <span className="mt-0.5 block truncate text-[0.72rem] text-faint">
                      {entry.is_dir ? "文件夹" : formatBytes(entry.size)} · 设备时间{" "}
                      {formatDateTime(entry.deviceTimeMs)}
                      {entry.hidden ? " · 隐藏" : ""}
                    </span>
                  </span>
                </button>
                <div className="flex shrink-0 items-center space-x-0.5">
                  {entry.is_dir ? (
                    <IconAction label="打开" icon={FolderClosed} disabled={busy} onClick={() => onOpen(entry)} />
                  ) : (
                    <IconAction label="下载" icon={Download} disabled={busy} onClick={() => onDownload(entry)} />
                  )}
                  <IconAction label="重命名" icon={Pencil} disabled={busy} onClick={() => onRename(entry)} />
                  <IconAction label="复制" icon={Copy} disabled={busy} onClick={() => onCopy(entry)} />
                  <IconAction label="移动" icon={FolderInput} disabled={busy} onClick={() => onMove(entry)} />
                  <IconAction label="删除" icon={Trash2} danger disabled={busy} onClick={() => onDelete(entry)} />
                </div>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <ul className="min-w-0 space-y-2.5 sm:space-y-3">
          {entries.map((entry) => (
            <li key={entry.path} className={cn("surface min-w-0 overflow-hidden", watch ? "p-2.5" : "p-3")}>
              <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2">
                <button
                  type="button"
                  title={entry.path}
                  onClick={() => (entry.is_dir ? onOpen(entry) : setSheetEntry(entry))}
                  className="tap-row flex min-w-0 items-center rounded-xl px-1 text-left"
                >
                  <span
                    className={cn(
                      "mr-2.5 flex shrink-0 items-center justify-center rounded-xl border border-line bg-sunken",
                      watch ? "size-9" : "size-10",
                      entry.is_dir ? "text-warn" : "text-faint",
                    )}
                  >
                    {entry.is_dir ? (
                      <FolderClosed className={watch ? "size-4" : "size-[1.1rem]"} aria-hidden />
                    ) : (
                      <FileIcon className={watch ? "size-4" : "size-[1.1rem]"} aria-hidden />
                    )}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span
                      className={cn(
                        "block truncate font-medium",
                        watch ? "text-[0.8rem]" : "text-[0.86rem]",
                        entry.hidden ? "text-faint" : "text-ink",
                      )}
                    >
                      {entry.name}
                    </span>
                    <span
                      className={cn(
                        "mt-0.5 block truncate text-faint",
                        watch ? "text-[0.72rem]" : "text-[0.74rem]",
                      )}
                    >
                      {entry.is_dir ? "文件夹" : formatBytes(entry.size)}
                      {entry.hidden ? " · 隐藏" : ""} · {formatDateTime(entry.deviceTimeMs)}
                    </span>
                  </span>
                </button>
                <button
                  type="button"
                  aria-label={`${entry.name} 的更多操作`}
                  title="更多操作"
                  onClick={() => setSheetEntry(entry)}
                  disabled={busy}
                  className="tap flex size-11 shrink-0 items-center justify-center rounded-xl border border-line bg-sunken text-soft disabled:pointer-events-none disabled:opacity-40"
                >
                  <MoreHorizontal className="size-4" aria-hidden />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <EntryActionsSheet
        entry={sheetEntry}
        busy={busy}
        onClose={() => setSheetEntry(null)}
        onOpen={onOpen}
        onRename={onRename}
        onCopy={onCopy}
        onMove={onMove}
        onDelete={onDelete}
        onDownload={onDownload}
      />
    </>
  );
}

function SheetAction({
  icon: Icon,
  label,
  disabled,
  danger = false,
  onClick,
}: {
  icon: typeof Download;
  label: string;
  disabled?: boolean;
  danger?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "tap-row flex w-full min-w-0 items-center rounded-xl border px-3 text-left transition-colors disabled:pointer-events-none disabled:opacity-40",
        danger
          ? "border-line bg-danger-soft text-danger"
          : "border-line bg-sunken text-ink hover:border-line-strong",
      )}
    >
      <Icon className="mr-2.5 size-4 shrink-0" aria-hidden />
      <span className="min-w-0 flex-1 truncate text-[0.82rem] font-medium">{label}</span>
    </button>
  );
}

/** Bottom sheet (phone/watch) / small dialog (desktop) with every entry action. */
function EntryActionsSheet({
  entry,
  busy,
  onClose,
  onOpen,
  onRename,
  onCopy,
  onMove,
  onDelete,
  onDownload,
}: {
  entry: Entry | null;
  busy: boolean;
  onClose: () => void;
  onOpen: (entry: Entry) => void;
  onRename: (entry: Entry) => void;
  onCopy: (entry: Entry) => void;
  onMove: (entry: Entry) => void;
  onDelete: (entry: Entry) => void;
  onDownload: (entry: Entry) => void;
}) {
  const viewport = useViewport();
  const watch = viewport === "watch";

  const run = (handler: (entry: Entry) => void) => {
    if (!entry) return;
    const target = entry;
    onClose();
    handler(target);
  };

  return (
    <Dialog
      open={entry !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent size="sm" className={watch ? "p-4" : undefined}>
        <DialogHeader>
          <DialogTitle className="truncate pr-6">{entry?.name ?? ""}</DialogTitle>
          <DialogDescription className="break-all">{entry?.path ?? ""}</DialogDescription>
        </DialogHeader>
        {entry ? (
          <>
            <dl className="grid min-w-0 grid-cols-2 gap-2 rounded-xl border border-line bg-sunken p-2.5 text-[0.72rem]">
              <div className="min-w-0">
                <dt className="text-faint">类型</dt>
                <dd className="mt-0.5 truncate text-ink">
                  {entry.is_dir ? "文件夹" : "文件"}
                  {entry.hidden ? " · 隐藏" : ""}
                </dd>
              </div>
              <div className="min-w-0">
                <dt className="text-faint">大小</dt>
                <dd className="tnum mt-0.5 truncate text-ink">
                  {entry.is_dir ? "—" : formatBytes(entry.size)}
                </dd>
              </div>
              <div className="col-span-2 min-w-0">
                <dt className="text-faint">设备时间</dt>
                <dd className="tnum mt-0.5 truncate text-ink">{formatDateTime(entry.deviceTimeMs)}</dd>
              </div>
            </dl>
            <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
              {entry.is_dir ? (
                <SheetAction icon={FolderClosed} label="打开文件夹" disabled={busy} onClick={() => run(onOpen)} />
              ) : (
                <SheetAction icon={Download} label="下载" disabled={busy} onClick={() => run(onDownload)} />
              )}
              <SheetAction icon={Pencil} label="重命名" disabled={busy} onClick={() => run(onRename)} />
              <SheetAction icon={Copy} label="复制到…" disabled={busy} onClick={() => run(onCopy)} />
              <SheetAction icon={FolderInput} label="移动到…" disabled={busy} onClick={() => run(onMove)} />
              <SheetAction icon={Trash2} label="删除" danger disabled={busy} onClick={() => run(onDelete)} />
            </div>
          </>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function ActionDialog({
  action,
  value,
  recursive,
  busy,
  basePath,
  onChange,
  onRecursiveChange,
  onCancel,
  onSubmit,
}: {
  action: ActionState | null;
  value: string;
  recursive: boolean;
  busy: boolean;
  basePath: string;
  onChange: (value: string) => void;
  onRecursiveChange: (value: boolean) => void;
  onCancel: () => void;
  onSubmit: () => void;
}) {
  const kind = action?.kind;
  const title =
    kind === "mkdir"
      ? "新建文件夹"
      : kind === "rename"
        ? "重命名"
        : kind === "copy"
          ? "复制到"
          : kind === "move"
            ? "移动到"
            : "搜索";
  const hint =
    kind === "mkdir"
      ? `将在 ${basePath || "（驱动器）"} 下创建`
      : kind === "rename"
        ? "输入新的名称"
        : kind === "copy" || kind === "move"
          ? "输入目标的完整路径（含文件名）"
          : `在 ${basePath || "（驱动器）"} 下按文件名子串搜索`;
  return (
    <Dialog
      open={action !== null}
      onOpenChange={(open) => {
        if (!open) onCancel();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{hint}</DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <Label htmlFor="file-action-input" className="text-xs text-soft">
            {kind === "search" ? "匹配片段" : kind === "copy" || kind === "move" ? "目标路径" : "名称"}
          </Label>
          <Input
            id="file-action-input"
            value={value}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") onSubmit();
            }}
            autoFocus
            className="h-11"
          />
          {kind === "search" ? (
            <label className="tap flex items-center rounded-lg border border-line bg-sunken px-3 text-xs text-soft">
              <input
                type="checkbox"
                className="mr-2 size-4"
                checked={recursive}
                onChange={(event) => onRecursiveChange(event.target.checked)}
              />
              递归搜索子目录
            </label>
          ) : null}
        </div>
        <DialogFooter>
          <Button variant="outline" className="tap" onClick={onCancel} disabled={busy}>
            取消
          </Button>
          <Button className="tap" onClick={onSubmit} disabled={busy || !value.trim()}>
            {busy ? "处理中…" : kind === "search" ? "搜索" : "确定"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
