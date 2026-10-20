import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowUp,
  Download,
  File as FileIcon,
  Folder,
  FolderPlus,
  HardDrive,
  Pencil,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import { basename, dirname, formatBytes, formatDateTime } from "@/lib/format";
import type { Device, FileEntry } from "@/lib/types";
import { cn } from "@/lib/utils";

const CHUNK = 1024 * 1024;

function errorText(failure: unknown): string {
  return failure instanceof ApiError ? failure.detail : failure instanceof Error ? failure.message : "操作失败";
}

export function FilesView({ device }: { device: Device }) {
  const [path, setPath] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [selected, setSelected] = useState<FileEntry | null>(null);
  const [searchPattern, setSearchPattern] = useState("");
  const [searchResults, setSearchResults] = useState<FileEntry[] | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const canWrite = device.presence.capabilities.files;
  const query = useQuery({
    queryKey: ["files", device.uid, path],
    queryFn: () => api.files(device.uid, path),
    enabled: device.presence.capabilities.files,
    retry: 0,
  });

  const run = async (label: string, action: () => Promise<void>) => {
    setBusy(label);
    setError("");
    try {
      await action();
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy("");
    }
  };

  const refresh = async () => {
    await query.refetch();
  };

  const open = (entry: FileEntry) => {
    if (entry.is_dir) {
      setPath(entry.path);
      setSelected(null);
      setSearchResults(null);
    } else {
      setSelected(entry);
    }
  };

  const createFolder = () =>
    run("mkdir", async () => {
      const name = window.prompt("新建文件夹名称");
      if (!name) return;
      const target = path ? `${path.replace(/[\\/]+$/, "")}\\${name}` : name;
      const result = await api.fileOp(device.uid, { op: "mkdir", path: target });
      if (!result.ok) throw new Error(result.error ?? "创建失败");
      await refresh();
    });

  const rename = (entry: FileEntry) =>
    run("rename", async () => {
      const name = window.prompt("重命名为", entry.name);
      if (!name || name === entry.name) return;
      const result = await api.fileOp(device.uid, { op: "rename", path: entry.path, target: name });
      if (!result.ok) throw new Error(result.error ?? "重命名失败");
      setSelected(null);
      await refresh();
    });

  const remove = (entry: FileEntry) =>
    run("delete", async () => {
      if (!window.confirm(`删除 ${entry.name}？${entry.is_dir ? "（含全部内容）" : ""}`)) return;
      const result = await api.fileOp(device.uid, { op: "delete", path: entry.path, recursive: entry.is_dir });
      if (!result.ok) throw new Error(result.error ?? "删除失败");
      setSelected(null);
      await refresh();
    });

  const download = (entry: FileEntry) =>
    run("download", async () => {
      const parts: BlobPart[] = [];
      let offset = 0;
      for (;;) {
        const chunk = await api.readChunk(device.uid, entry.path, offset, CHUNK);
        parts.push(chunk.blob);
        offset += chunk.blob.size;
        if (chunk.eof || chunk.blob.size === 0) break;
      }
      const blob = new Blob(parts);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = entry.name;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 4000);
    });

  const upload = (file: File) =>
    run(`upload ${file.name}`, async () => {
      const target = `${path.replace(/[\\/]+$/, "")}\\${file.name}`;
      let offset = 0;
      while (offset < file.size) {
        const chunk = file.slice(offset, offset + CHUNK);
        const result = await api.writeChunk(device.uid, target, offset, chunk, offset === 0);
        if (!result.ok) throw new Error(result.error ?? "写入失败");
        offset += chunk.size;
      }
      await refresh();
    });

  const search = () =>
    run("search", async () => {
      const root = path || "C:\\";
      const result = await api.fileOp(device.uid, {
        op: "search",
        path: root,
        pattern: searchPattern || "*",
        recursive: true,
      });
      if (!result.ok) throw new Error(result.error ?? "搜索失败");
      const data = result.data as { entries?: FileEntry[] };
      setSearchResults(data.entries ?? []);
    });

  if (!canWrite) {
    return (
      <p className="rounded-panel border border-warn/30 bg-warn/10 px-3 py-2 text-xs leading-relaxed text-warn">
        文件管理需要受控端在线（"受控"能力是否可用见设备状态）：{device.presence.reason}
      </p>
    );
  }

  const drives = query.data?.drives ?? [];
  const entries = searchResults ?? query.data?.entries ?? [];
  const parent = searchResults ? null : (query.data?.parent ?? (path ? dirname(path) : null));

  return (
    <div className="space-y-2">
      {error ? (
        <p className="rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger">{error}</p>
      ) : null}

      <div className="flex flex-wrap items-center gap-1.5">
        <Button
          variant="outline"
          size="icon-sm"
          disabled={!path}
          onClick={() => {
            setPath(parent ?? "");
            setSearchResults(null);
            setSelected(null);
          }}
          aria-label="上一级"
        >
          <ArrowUp className="size-3.5" />
        </Button>
        <Button variant="outline" size="icon-sm" onClick={() => void query.refetch()} aria-label="刷新">
          <HardDrive className="size-3.5" />
        </Button>
        <div className="min-w-0 flex-1 truncate rounded-md border border-line bg-sunken px-2 py-1 font-mono text-[0.68rem] text-soft">
          {searchResults ? `搜索结果（${searchResults.length}）` : path || "此电脑"}
        </div>
        <Button variant="outline" size="icon-sm" onClick={createFolder} disabled={!path || Boolean(busy)} aria-label="新建文件夹">
          <FolderPlus className="size-3.5" />
        </Button>
        <Button
          variant="outline"
          size="icon-sm"
          onClick={() => fileInput.current?.click()}
          disabled={!path || Boolean(busy)}
          aria-label="上传文件"
        >
          <Upload className="size-3.5" />
        </Button>
        <input
          ref={fileInput}
          type="file"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void upload(file);
          }}
        />
      </div>

      <div className="flex items-center gap-1.5">
        <Input
          value={searchPattern}
          onChange={(event) => setSearchPattern(event.target.value)}
          placeholder="在当前目录中搜索名称…"
          className="h-8 text-xs"
        />
        <Button variant="outline" size="sm" onClick={search} disabled={Boolean(busy)}>
          <Search className="size-3.5" />
          搜索
        </Button>
        {searchResults ? (
          <Button variant="ghost" size="icon-sm" onClick={() => setSearchResults(null)} aria-label="退出搜索">
            <X className="size-3.5" />
          </Button>
        ) : null}
      </div>

      {busy ? <p className="text-[0.68rem] text-brand">正在{busy}…</p> : null}

      {!path && !searchResults ? (
        <ul className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
          {drives.map((drive) => (
            <li key={drive.path}>
              <button
                type="button"
                onClick={() => open(drive)}
                className="flex w-full items-center gap-2 rounded-lg border border-line bg-panel px-3 py-2.5 text-left"
              >
                <HardDrive className="size-4 shrink-0 text-brand" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[0.78rem] font-medium text-ink">{drive.name}</span>
                  <span className="block truncate text-[0.66rem] text-faint">
                    {drive.filesystem || "—"} · 剩余 {formatBytes(drive.free)} / {formatBytes(drive.size)}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : query.isLoading ? (
        <div className="space-y-1.5">
          {Array.from({ length: 8 }).map((_, index) => (
            <Skeleton key={index} className="h-10 w-full rounded-md" />
          ))}
        </div>
      ) : entries.length === 0 ? (
        <p className="rounded-panel border border-dashed border-line px-3 py-10 text-center text-xs text-soft">
          这个目录是空的
        </p>
      ) : (
        <ul className="divide-y divide-line overflow-hidden rounded-panel border border-line bg-panel">
          {entries.map((entry) => (
            <li key={entry.path}>
              <button
                type="button"
                onClick={() => open(entry)}
                onDoubleClick={() => open(entry)}
                className={cn(
                  "flex min-h-11 w-full items-center gap-2 px-2.5 py-1.5 text-left",
                  selected?.path === entry.path && "bg-brand-soft",
                )}
              >
                {entry.is_dir ? (
                  <Folder className="size-4 shrink-0 text-brand" />
                ) : (
                  <FileIcon className="size-4 shrink-0 text-soft" />
                )}
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[0.78rem] text-ink">{entry.name}</span>
                  <span className="block truncate text-[0.64rem] text-faint">
                    {entry.is_dir ? "文件夹" : formatBytes(entry.size)} · {formatDateTime(entry.mtime_ms)}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {query.data?.truncated ? (
        <p className="text-[0.66rem] text-faint">目录内容过多，只显示前 {entries.length} 项。</p>
      ) : null}

      {selected ? (
        <div className="fixed inset-x-0 bottom-0 z-40 border-t border-line bg-panel px-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))] pt-3 shadow-raised">
          <div className="mx-auto flex max-w-3xl items-center gap-2">
            <span className="min-w-0 flex-1 truncate text-[0.72rem] text-soft" title={selected.path}>
              {basename(selected.path)} · {formatBytes(selected.size)}
            </span>
            <Button size="sm" variant="outline" onClick={() => download(selected)} disabled={Boolean(busy)}>
              <Download className="size-3.5" />
              下载
            </Button>
            <Button size="sm" variant="outline" onClick={() => rename(selected)} disabled={Boolean(busy)}>
              <Pencil className="size-3.5" />
              重命名
            </Button>
            <Button size="sm" variant="destructive" onClick={() => remove(selected)} disabled={Boolean(busy)}>
              <Trash2 className="size-3.5" />
              删除
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setSelected(null)} aria-label="关闭">
              <X className="size-3.5" />
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  );
}