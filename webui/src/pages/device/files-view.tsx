import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUp,
  Copy,
  Download,
  File as FileIcon,
  FolderClosed,
  FolderInput,
  FolderPlus,
  HardDrive,
  Pencil,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";

import { Banner, EmptyBlock, ErrorBlock, LoadingBlock } from "@/components/async-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
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
      <div className="space-y-2">
        <Banner tone="warn">
          <p className="font-medium">文件管理当前不可用</p>
          <p className="mt-0.5 font-normal">
            服务端 capability 已关闭：{device.presence.reason || "设备没有活动链路"}。文件管理需要受控端有活动通道；
            恢复后本页会自动可用。
          </p>
        </Banner>
        <EmptyBlock
          title="没有可浏览的文件"
          description="受控端当前不在线（或仅能被服务器轮询但未建立命令通道），因此不会展示可能过期的目录内容。"
        />
      </div>
    );
  }

  const listing = search ?? null;

  return (
    <div className="space-y-2">
      {message ? <Banner tone="ok">{message}</Banner> : null}
      {error ? <Banner tone="error">{error}</Banner> : null}
      {progress ? (
        <div className="rounded-panel border border-line bg-panel p-2.5">
          <div className="mb-1 flex items-center justify-between gap-2 text-xs text-soft">
            <span className="truncate">
              {progress.label} · {formatBytes(progress.loaded)} / {formatBytes(progress.total)}
            </span>
            <Button size="sm" variant="ghost" onClick={() => abortRef.current?.abort()}>
              <X className="size-3.5" />
              取消
            </Button>
          </div>
          <Progress value={progress.total > 0 ? Math.round((progress.loaded / progress.total) * 100) : 0} />
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-1.5">
        <Button size="sm" variant="outline" onClick={() => void refresh()} disabled={listQuery.isFetching || Boolean(busy)}>
          <RefreshCw className={cn("size-3.5", listQuery.isFetching && "animate-spin")} />
          刷新
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => setPath(parent ?? "")}
          disabled={!path}
        >
          <ArrowUp className="size-3.5" />
          上级
        </Button>
        <Button size="sm" variant="outline" onClick={() => openAction("mkdir")} disabled={Boolean(busy)}>
          <FolderPlus className="size-3.5" />
          新建文件夹
        </Button>
        <Button size="sm" variant="outline" onClick={() => openAction("search")} disabled={Boolean(busy)}>
          <Search className="size-3.5" />
          搜索
        </Button>
        <Button size="sm" onClick={() => uploadRef.current?.click()} disabled={Boolean(busy) || !path}>
          <Upload className="size-3.5" />
          上传
        </Button>
        <input
          ref={uploadRef}
          type="file"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void upload(file);
          }}
        />
        {search ? (
          <Button size="sm" variant="ghost" onClick={() => setSearch(null)}>
            退出搜索结果
          </Button>
        ) : null}
      </div>

      <div className="flex flex-wrap items-center gap-1 text-xs text-soft">
        {crumbs(path).map((crumb, index) => (
          <span key={`${crumb.path}-${index}`} className="flex items-center gap-1">
            {index > 0 ? <span className="text-faint">›</span> : null}
            <button
              type="button"
              onClick={() => {
                setSearch(null);
                setPath(crumb.path);
              }}
              className={cn(
                "rounded px-1 py-0.5 hover:bg-sunken",
                index === crumbs(path).length - 1 && "font-medium text-ink",
              )}
            >
              {crumb.label}
            </button>
          </span>
        ))}
      </div>

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
          <div className="grid gap-2 sm:grid-cols-2">
            {drives.map((drive) => (
              <button
                key={drive.path}
                type="button"
                onClick={() => setPath(drive.path)}
                className="flex items-center gap-2.5 rounded-panel border border-line bg-panel p-4 text-left hover:bg-sunken"
              >
                <HardDrive className="size-5 shrink-0 text-soft" aria-hidden />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium text-ink">{drive.name}</span>
                  <span className="block text-xs text-soft">
                    {drive.filesystem || "—"} · 剩余 {drive.free === null ? "—" : formatBytes(drive.free)}
                    {drive.total === null ? "" : ` / ${formatBytes(drive.total)}`}
                  </span>
                </span>
              </button>
            ))}
          </div>
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

      <p className="text-[0.7rem] leading-relaxed text-faint">
        当前路径：<span className="font-mono">{path || "（驱动器列表）"}</span> · 分片 {CHUNK_BYTES / 1024} KiB ·
        单文件上传上限由服务端设置（默认 512 MiB）
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
  return (
    <ul className="divide-y divide-line rounded-panel border border-line bg-panel">
      {entries.map((entry) => (
        <li key={entry.path} className="flex items-center gap-2 px-2.5 py-2">
          <button
            type="button"
            onClick={() => onOpen(entry)}
            className="flex min-w-0 flex-1 items-center gap-2 text-left"
            title={entry.path}
          >
            {entry.is_dir ? (
              <FolderClosed className="size-4 shrink-0 text-warn" aria-hidden />
            ) : (
              <FileIcon className="size-4 shrink-0 text-faint" aria-hidden />
            )}
            <span className="min-w-0 flex-1">
              <span className={cn("block truncate text-[0.8rem]", entry.hidden ? "text-faint" : "text-ink")}>
                {entry.name}
              </span>
              <span className="block truncate text-[0.7rem] text-faint">
                {entry.is_dir ? "文件夹" : formatBytes(entry.size)} · 设备时间 {formatDateTime(entry.deviceTimeMs)}
              </span>
            </span>
          </button>
          <div className="flex shrink-0 items-center gap-0.5">
            {!entry.is_dir ? (
              <Button size="icon-sm" variant="ghost" title="下载" disabled={busy} onClick={() => onDownload(entry)}>
                <Download className="size-3.5" />
              </Button>
            ) : (
              <Button size="icon-sm" variant="ghost" title="打开" disabled={busy} onClick={() => onOpen(entry)}>
                <FolderClosed className="size-3.5" />
              </Button>
            )}
            <Button size="icon-sm" variant="ghost" title="重命名" disabled={busy} onClick={() => onRename(entry)}>
              <Pencil className="size-3.5" />
            </Button>
            <Button size="icon-sm" variant="ghost" title="复制" disabled={busy} onClick={() => onCopy(entry)}>
              <Copy className="size-3.5" />
            </Button>
            <Button size="icon-sm" variant="ghost" title="移动" disabled={busy} onClick={() => onMove(entry)}>
              <FolderInput className="size-3.5" />
            </Button>
            <Button size="icon-sm" variant="ghost" title="删除" disabled={busy} onClick={() => onDelete(entry)}>
              <Trash2 className="size-3.5 text-danger" />
            </Button>
          </div>
        </li>
      ))}
    </ul>
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
            className="h-9"
          />
          {kind === "search" ? (
            <label className="flex items-center gap-1.5 text-xs text-soft">
              <input type="checkbox" checked={recursive} onChange={(event) => onRecursiveChange(event.target.checked)} />
              递归搜索子目录
            </label>
          ) : null}
        </div>
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