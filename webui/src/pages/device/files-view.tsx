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

import { Banner, EmptyBlock, ErrorBlock, LoadingBlock } from "@/components/async-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { Meter } from "@/components/meter";
import { Panel } from "@/components/panel";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
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
      setError(
        isApiError(failure) ? failure.message : failure instanceof Error ? failure.message : `${label}失败`,
      );
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
        const raw = Array.isArray(result.data.entries)
          ? (result.data.entries as Record<string, unknown>[])
          : [];
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
        setError(
          isApiError(failure) ? failure.message : failure instanceof Error ? failure.message : "上传失败",
        );
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
        <EmptyBlock title="没有可浏览的文件" description="受控端恢复活动通道后本页会自动可用。" />
      </div>
    );
  }

  const listing = search ?? null;
  const crumbList = crumbs(path);
  const progressPercent =
    progress && progress.total > 0
      ? Math.min(100, Math.round((progress.loaded / progress.total) * 100))
      : 0;

  const actions = {
    onRename: (entry: Entry) => openAction("rename", entry),
    onCopy: (entry: Entry) => openAction("copy", entry),
    onMove: (entry: Entry) => openAction("move", entry),
    onDelete: setPendingDelete,
    onDownload: (entry: Entry) => void download(entry),
  };

  return (
    <div
      className={cn(
        "w-full max-w-full min-w-0 overflow-x-hidden",
        watch ? "space-y-3" : "space-y-4",
      )}
    >
      {/* Toolbar: the only controls, one compact row per breakpoint. */}
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Button
            className="gap-2"
            variant="outline"
            onClick={() => void refresh()}
            disabled={listQuery.isFetching || Boolean(busy)}
          >
            <RefreshCw
              className={cn("size-4 stroke-[1.5]", listQuery.isFetching && "animate-spin")}
              aria-hidden
            />
            刷新
          </Button>
          <Button
            className="gap-2"
            variant="outline"
            onClick={() => setPath(parent ?? "")}
            disabled={!path}
          >
            <ArrowUp className="size-4 stroke-[1.5]" aria-hidden />
            上级
          </Button>
          <Button
            className="gap-2"
            variant="outline"
            onClick={() => openAction("mkdir")}
            disabled={Boolean(busy)}
          >
            <FolderPlus className="size-4 stroke-[1.5]" aria-hidden />
            新建
          </Button>
          <Button
            className="gap-2"
            variant="secondary"
            onClick={() => openAction("search")}
            disabled={Boolean(busy)}
          >
            <Search className="size-4 stroke-[1.5]" aria-hidden />
            搜索
          </Button>
        </div>
        <div className="flex items-center gap-2">
          {search ? (
            <Button variant="ghost" onClick={() => setSearch(null)}>
              退出搜索
            </Button>
          ) : null}
          <Button
            className="gap-2"
            disabled={Boolean(busy) || !path}
            onClick={() => uploadRef.current?.click()}
          >
            <Upload className="size-4 stroke-[1.5]" aria-hidden />
            上传
          </Button>
        </div>
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

      {/* In-flight transfer: progress + the only cancel affordance. */}
      {progress ? (
        <div className="min-w-0 rounded-md border border-border bg-card px-3 py-2.5">
          <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2">
            <div className="min-w-0">
              <p className={cn("truncate font-medium text-foreground", watch ? "text-xs" : "text-sm")}>
                {progress.label}
              </p>
              <p className="tnum mt-0.5 truncate text-xs text-muted-foreground">
                {formatBytes(progress.loaded)} / {formatBytes(progress.total)} · {progressPercent}%
              </p>
            </div>
            <Button size="sm" variant="outline" onClick={() => abortRef.current?.abort()}>
              <X className="size-3.5" aria-hidden />
              取消
            </Button>
          </div>
          <Progress className="mt-2.5" value={progressPercent} />
        </div>
      ) : null}

      {message ? <Banner tone="ok">{message}</Banner> : null}
      {error ? <Banner tone="error">{error}</Banner> : null}

      {/* The only horizontally scrolling row on this page: the breadcrumb.
          At the drive root the panel header already names the level. */}
      {path || search ? (
      <Breadcrumb className="min-w-0 max-w-full">
        <BreadcrumbList className="hide-scrollbar flex-nowrap overflow-x-auto">
          <FolderClosed className="size-3.5 shrink-0 text-faint stroke-[1.5]" aria-hidden />
          {crumbList.map((crumb, index) => {
            const last = index === crumbList.length - 1;
            return (
              <BreadcrumbItem key={`${crumb.path}-${index}`} className="shrink-0">
                {index > 0 ? <BreadcrumbSeparator /> : null}
                {last ? (
                  <BreadcrumbPage className="max-w-[12rem] truncate px-1.5 py-1">{crumb.label}</BreadcrumbPage>
                ) : (
                  <button
                    type="button"
                    onClick={() => {
                      setSearch(null);
                      setPath(crumb.path);
                    }}
                    className="max-w-[12rem] truncate rounded-sm px-1.5 py-1 transition-colors hover:bg-accent hover:text-foreground"
                  >
                    {crumb.label}
                  </button>
                )}
              </BreadcrumbItem>
            );
          })}
        </BreadcrumbList>
      </Breadcrumb>
      ) : null}

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
              busy={Boolean(busy)}
              {...actions}
            />
          )}
        </div>
      ) : !path ? (
        drives.length === 0 ? (
          <EmptyBlock title="没有可用的驱动器" description="受控端没有返回任何逻辑驱动器。" />
        ) : (
          <Panel title="驱动器" hint="选择逻辑驱动器开始浏览" flush>
            <ul>
              {drives.map((drive, index) => {
                const percent =
                  drive.free !== null && drive.total !== null && drive.total > 0
                    ? (drive.free / drive.total) * 100
                    : null;
                return (
                  <li key={drive.path} className="min-w-0">
                    {index > 0 ? <div className="mx-4 border-t border-border" /> : null}
                    <div className="flex min-w-0 items-center gap-3 px-6 py-4">
                      <HardDrive
                        className="size-4 shrink-0 stroke-[1.5] text-muted-foreground"
                        aria-hidden
                      />
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm text-foreground">{drive.name}</p>
                        <p className="mt-1 truncate text-xs text-muted-foreground">
                          {drive.filesystem || "未知文件系统"} · {drive.path}
                        </p>
                        <div className="mt-2.5 max-w-xs">
                          {percent === null ? (
                            <p className="text-xs text-faint">剩余空间：受控端未上报</p>
                          ) : (
                            <Meter
                              compact
                              tone="ok"
                              label="剩余空间"
                              value={percent}
                              hint={`${formatBytes(drive.free ?? 0)} 可用 / ${formatBytes(drive.total ?? 0)}`}
                            />
                          )}
                        </div>
                      </div>
                      <Button
                        variant="outline"
                        className="shrink-0 gap-1"
                        onClick={() => setPath(drive.path)}
                      >
                        浏览
                        <ChevronRight className="size-4 shrink-0 stroke-[1.5]" aria-hidden />
                      </Button>
                    </div>
                  </li>
                );
              })}
            </ul>
          </Panel>
        )
      ) : entries.length === 0 ? (
        <EmptyBlock title="这个目录是空的" description={path ? `路径：${path}` : undefined} />
      ) : (
        <EntryList
          entries={entries}
          onOpen={(entry) => {
            if (entry.is_dir) setPath(entry.path);
          }}
          busy={Boolean(busy)}
          {...actions}
        />
      )}

      {listQuery.data?.truncated ? <Banner tone="warn">目录条目过多，受控端已截断返回。</Banner> : null}

      <p className="px-0 text-xs leading-relaxed text-muted-foreground">
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

/**
 * One directory listing.
 *
 * Desktop keeps a compact table with a row menu; phone/watch get full-width
 * tappable cards whose actions live in a bottom sheet (`.ui-dialog` turns into
 * one automatically), so no row squeezes five tiny icons.
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
        <Panel flush className="min-w-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>名称</TableHead>
                <TableHead className="w-28">大小</TableHead>
                <TableHead className="w-40">设备时间</TableHead>
                <TableHead className="w-16 pr-6 text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {entries.map((entry) => (
                <TableRow key={entry.path} className="h-14 hover:bg-muted">
                  <TableCell className="max-w-0">
                    <button
                      type="button"
                      title={entry.path}
                      onClick={() => (entry.is_dir ? onOpen(entry) : setSheetEntry(entry))}
                      className="flex min-w-0 max-w-full items-center gap-2.5 text-left"
                    >
                      <span
                        className={cn(
                          "flex size-7 shrink-0 items-center justify-center rounded-sm border border-border bg-muted",
                          entry.is_dir ? "text-warning" : "text-faint",
                        )}
                      >
                        {entry.is_dir ? (
                          <FolderClosed className="size-3.5 stroke-[1.5]" aria-hidden />
                        ) : (
                          <FileIcon className="size-3.5 stroke-[1.5]" aria-hidden />
                        )}
                      </span>
                      <span className={cn("min-w-0 truncate text-sm font-medium", entry.hidden ? "text-faint" : "text-foreground")}>
                        {entry.name}
                      </span>
                      {entry.hidden ? (
                        <Badge variant="neutral" className="shrink-0 text-xs">
                          隐藏
                        </Badge>
                      ) : null}
                    </button>
                  </TableCell>
                  <TableCell className="tnum text-xs text-muted-foreground">
                    {entry.is_dir ? "—" : formatBytes(entry.size)}
                  </TableCell>
                  <TableCell className="tnum text-xs text-muted-foreground">
                    {formatDateTime(entry.deviceTimeMs)}
                  </TableCell>
                  <TableCell className="pr-6 text-right">
                    <DropdownMenu>
                      <DropdownMenuTrigger
                        render={
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            aria-label={`${entry.name} 的更多操作`}
                            disabled={busy}
                          />
                        }
                      >
                        <MoreHorizontal className="size-4" />
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        {entry.is_dir ? (
                          <DropdownMenuItem onClick={() => onOpen(entry)}>
                            <FolderClosed className="size-4" />
                            打开
                          </DropdownMenuItem>
                        ) : (
                          <DropdownMenuItem onClick={() => onDownload(entry)}>
                            <Download className="size-4" />
                            下载
                          </DropdownMenuItem>
                        )}
                        <DropdownMenuItem onClick={() => onRename(entry)}>
                          <Pencil className="size-4" />
                          重命名
                        </DropdownMenuItem>
                        <DropdownMenuItem onClick={() => onCopy(entry)}>
                          <Copy className="size-4" />
                          复制到…
                        </DropdownMenuItem>
                        <DropdownMenuItem onClick={() => onMove(entry)}>
                          <FolderInput className="size-4" />
                          移动到…
                        </DropdownMenuItem>
                        <DropdownMenuSeparator />
                        <DropdownMenuItem variant="destructive" onClick={() => onDelete(entry)}>
                          <Trash2 className="size-4" />
                          删除
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Panel>
      ) : (
        <div className="min-w-0 overflow-hidden rounded-md border border-border bg-card">
          <ul className="divide-y divide-border">
            {entries.map((entry) => (
              <li key={entry.path} className={cn("min-w-0", watch ? "px-2.5 py-2" : "px-3 py-2.5")}>
                <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2">
                  <button
                    type="button"
                    title={entry.path}
                    onClick={() => (entry.is_dir ? onOpen(entry) : setSheetEntry(entry))}
                    className="flex min-w-0 items-center gap-2.5 rounded-sm px-1 text-left"
                  >
                    <span
                      className={cn(
                        "flex shrink-0 items-center justify-center rounded-sm",
                        watch ? "size-8" : "size-9",
                        entry.is_dir ? "text-warning" : "text-muted-foreground",
                      )}
                    >
                      {entry.is_dir ? (
                        <FolderClosed className="size-4 stroke-[1.5]" aria-hidden />
                      ) : (
                        <FileIcon className="size-4 stroke-[1.5]" aria-hidden />
                      )}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span
                        className={cn(
                          "block truncate font-medium",
                          watch ? "text-sm" : "text-sm",
                          entry.hidden ? "text-faint" : "text-foreground",
                        )}
                      >
                        {entry.name}
                      </span>
                      <span
                        className={cn(
                          "mt-0.5 block truncate text-faint",
                          watch ? "text-xs" : "text-xs",
                        )}
                      >
                        {entry.is_dir ? "文件夹" : formatBytes(entry.size)}
                        {entry.hidden ? " · 隐藏" : ""} · {formatDateTime(entry.deviceTimeMs)}
                      </span>
                    </span>
                  </button>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`${entry.name} 的更多操作`}
                    title="更多操作"
                    onClick={() => setSheetEntry(entry)}
                    disabled={busy}
                  >
                    <MoreHorizontal className="size-4" aria-hidden />
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        </div>
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
        "tap-row flex w-full min-w-0 items-center gap-2.5 rounded-md border px-3 text-left transition-colors disabled:pointer-events-none disabled:opacity-40",
        danger
          ? "border-border bg-destructive-soft text-destructive hover:border-destructive"
          : "border-border bg-card text-foreground hover:bg-accent",
      )}
    >
      <Icon className="size-4 shrink-0" aria-hidden />
      <span className="min-w-0 flex-1 truncate text-sm font-medium">{label}</span>
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
            <dl className="grid min-w-0 grid-cols-2 gap-2 rounded-md border border-border bg-muted p-2.5 text-xs">
              <div className="min-w-0">
                <dt className="text-faint">类型</dt>
                <dd className="mt-0.5 truncate text-foreground">
                  {entry.is_dir ? "文件夹" : "文件"}
                  {entry.hidden ? " · 隐藏" : ""}
                </dd>
              </div>
              <div className="min-w-0">
                <dt className="text-faint">大小</dt>
                <dd className="tnum mt-0.5 truncate text-foreground">
                  {entry.is_dir ? "—" : formatBytes(entry.size)}
                </dd>
              </div>
              <div className="col-span-2 min-w-0">
                <dt className="text-faint">设备时间</dt>
                <dd className="tnum mt-0.5 truncate text-foreground">
                  {formatDateTime(entry.deviceTimeMs)}
                </dd>
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
        <Field>
          <FieldLabel htmlFor="file-action-input">
            {kind === "search" ? "匹配片段" : kind === "copy" || kind === "move" ? "目标路径" : "名称"}
          </FieldLabel>
          <Input
            id="file-action-input"
            value={value}
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") onSubmit();
            }}
            autoFocus
            className="h-10"
          />
        </Field>
        {kind === "search" ? (
          <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-muted px-3 py-2">
            <span className="text-xs text-muted-foreground">递归搜索子目录</span>
            <Switch checked={recursive} onCheckedChange={onRecursiveChange} aria-label="递归搜索子目录" />
          </div>
        ) : null}
        <DialogFooter>
          <Button variant="outline" onClick={onCancel} disabled={busy}>
            取消
          </Button>
          <Button onClick={onSubmit} disabled={busy || !value.trim()}>
            {busy ? "处理中…" : kind === "search" ? "搜索" : "确定"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
