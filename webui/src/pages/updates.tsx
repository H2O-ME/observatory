import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PackageCheck, Trash2, Upload } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import { formatBytes, formatDateTime } from "@/lib/format";

export function UpdatesPage() {
  const query = useQuery({ queryKey: ["updates"], queryFn: api.updates, refetchInterval: 30_000 });
  const [notes, setNotes] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const upload = async (file: File) => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const pkg = await api.uploadPackage(file, notes);
      setMessage(`已上传 ${pkg.version}（${formatBytes(pkg.size_bytes)}），受控端会在下次心跳时自动更新`);
      setNotes("");
      await query.refetch();
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.detail : "上传失败");
    } finally {
      setBusy(false);
    }
  };

  const removePackage = async (version: string) => {
    if (!window.confirm(`删除 ${version} 安装包？`)) return;
    try {
      await api.deletePackage(version);
      await query.refetch();
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.detail : "删除失败");
    }
  };

  const data = query.data;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h1 className="text-base font-semibold text-ink">版本与更新</h1>
        <span className="font-mono text-[0.68rem] text-faint">服务器 v{data?.server_version ?? "—"}</span>
      </div>

      {message ? <p className="rounded-md border border-ok/30 bg-ok/10 px-2.5 py-1.5 text-xs text-ok">{message}</p> : null}
      {error ? <p className="rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger">{error}</p> : null}

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-1 text-xs font-semibold text-soft">自动更新</h2>
        <p className="text-[0.68rem] leading-relaxed text-faint">
          受控端只需安装一次。上传新的安装包后，旧版本设备会在心跳里收到版本信息，自行下载、校验 SHA-256、
          在服务停止后原子替换目录并重启——全程无 UAC、无需重新安装。
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <span className="rounded-md border border-line bg-sunken px-2 py-1 font-mono text-[0.68rem] text-soft">
            最新受控端：{data?.latest_agent_version ? `v${data.latest_agent_version}` : "未上传"}
          </span>
          <Button size="sm" variant="outline" onClick={() => fileInput.current?.click()} disabled={busy}>
            <Upload className="size-3.5" />
            {busy ? "上传中…" : "上传安装包"}
          </Button>
          <input
            ref={fileInput}
            type="file"
            accept=".zip"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (file) void upload(file);
            }}
          />
        </div>
        <Input
          value={notes}
          onChange={(event) => setNotes(event.target.value)}
          placeholder="安装包说明（可选）"
          className="mt-2 h-8 text-xs"
        />
        <p className="mt-1.5 text-[0.62rem] text-faint">
          文件名必须带版本号，例如 <span className="font-mono">observatory-agent-0.2.1.zip</span>。
        </p>
      </section>

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-2 text-xs font-semibold text-soft">已上传的版本</h2>
        {query.isLoading ? (
          <Skeleton className="h-16 w-full rounded-md" />
        ) : (data?.packages.length ?? 0) === 0 ? (
          <p className="rounded-md border border-dashed border-line px-3 py-6 text-center text-xs text-soft">
            还没有上传过受控端安装包
          </p>
        ) : (
          <ul className="divide-y divide-line">
            {(data?.packages ?? []).map((pkg) => (
              <li key={pkg.version} className="flex items-center gap-2 py-2">
                <PackageCheck className="size-4 shrink-0 text-brand" />
                <div className="min-w-0 flex-1">
                  <p className="text-[0.78rem] font-medium text-ink">
                    v{pkg.version}
                    {pkg.version === data?.latest_agent_version ? (
                      <span className="ml-1.5 rounded bg-brand-soft px-1 py-0.5 text-[0.6rem] text-brand">最新</span>
                    ) : null}
                  </p>
                  <p className="truncate text-[0.64rem] text-faint">
                    {formatBytes(pkg.size_bytes)} · {formatDateTime(pkg.created_at_ms)} ·{" "}
                    <span className="font-mono">{pkg.sha256.slice(0, 12)}…</span>
                  </p>
                </div>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  onClick={() => void removePackage(pkg.version)}
                  aria-label={`删除 ${pkg.version}`}
                >
                  <Trash2 className="size-3.5" />
                </Button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-2 text-xs font-semibold text-soft">待升级设备</h2>
        {(data?.outdated.length ?? 0) === 0 ? (
          <p className="text-[0.72rem] text-ok">
            {data?.latest_agent_version ? "所有设备都是最新版本" : "上传一个安装包后即可开始自动更新"}
          </p>
        ) : (
          <ul className="divide-y divide-line">
            {(data?.outdated ?? []).map((device) => (
              <li key={device.uid} className="flex items-center justify-between gap-2 py-1.5 text-[0.72rem]">
                <span className="min-w-0 truncate text-ink">{device.name}</span>
                <span className="shrink-0 text-faint">v{device.version}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}