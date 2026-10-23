import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, Copy, Download } from "lucide-react";

import { W2Notice } from "@/components/w2-notice";
import { Button, buttonVariants } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { api, isApiError } from "@/lib/api";
import { formatBytes, formatDuration } from "@/lib/format";
import { cn } from "@/lib/utils";

/**
 * Settings — W0 keeps the read-only half alive.
 *
 * The settings *form* is rebuilt in W2 (per-device overrides included, and with
 * a draft that cannot be wiped by a window-focus refetch). The system status and
 * the one-click installer are kept here because the wall's empty state links to
 * this page: without them a fresh install would have no entry point at all.
 * Everything shown comes from `GET /system`; nothing is inferred locally.
 */
export function SettingsPage() {
  const query = useQuery({ queryKey: ["system"], queryFn: api.getSystem, refetchInterval: 30_000 });
  const [copied, setCopied] = useState("");
  const [error, setError] = useState("");

  const system = query.data;
  const install = system?.install;

  const copy = async (text: string, tag: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(tag);
      window.setTimeout(() => setCopied(""), 2000);
    } catch {
      setError("复制失败，请手动选择文本");
    }
  };

  const copyInstallLink = (url: string, tag: string) => {
    const absolute = url.startsWith("http") ? url : `${location.origin}${url}`;
    void copy(absolute, tag);
  };

  if (query.isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-24 w-full rounded-panel" />
        <Skeleton className="h-64 w-full rounded-panel" />
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h1 className="text-base font-semibold text-ink">设置</h1>
        <span className="font-mono text-[0.68rem] text-faint">v{system?.version ?? "—"}</span>
      </div>

      {query.isError ? (
        <p role="alert" className="rounded-panel border border-danger/30 bg-danger/10 p-3 text-xs text-danger">
          无法读取系统状态：{isApiError(query.error) ? query.error.message : "未知错误"}
        </p>
      ) : null}
      {error ? (
        <p role="alert" className="rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger">
          {error}
        </p>
      ) : null}

      <W2Notice
        title="设置项表单（W2 重建）"
        description="采集、传输、实时控制、文件、更新、保留与归档等设置项的编辑表单在 W2 波次重建；值域校验仍由服务端唯一负责。"
        items={[
          "草稿不会被窗口聚焦刷新覆盖（W2 表单的硬性要求）",
          "per_device 覆盖项将在设备页提供入口",
          "密钥类设置只显示“已配置”，不会在浏览器里回显",
        ]}
        testId="settings-w2"
      />

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-2 text-xs font-semibold text-soft">服务器</h2>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[0.72rem]">
          <dt className="text-faint">版本</dt>
          <dd className="font-mono text-soft">v{system?.version ?? "—"}</dd>
          <dt className="text-faint">运行时长</dt>
          <dd className="text-soft">{formatDuration(system?.uptime_s)}</dd>
          <dt className="text-faint">设备</dt>
          <dd className="text-soft">
            {system?.devices ?? "—"} 台（WS {system?.channels.ws ?? "—"} / 轮询 {system?.channels.poll ?? "—"}）
          </dd>
          <dt className="text-faint">存储</dt>
          <dd className="text-soft">
            画面 {formatBytes(system?.storage.frames_bytes)} · 数据库 {formatBytes(system?.storage.db_bytes)} · 磁盘剩余{" "}
            {formatBytes(system?.storage.disk_free)}
          </dd>
          <dt className="text-faint">域名</dt>
          <dd className="break-all text-soft">{(system?.base_urls ?? []).join("  |  ") || "—"}</dd>
          <dt className="text-faint">待处理命令</dt>
          <dd className="text-soft">{system?.pending_commands ?? "—"}</dd>
        </dl>
      </section>

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-1 text-xs font-semibold text-soft">安装程序</h2>
        <p className="mb-2 text-[0.66rem] leading-relaxed text-faint">
          下载后双击运行即可，<b className="text-soft">不用输任何参数</b>：文件里已经带好注册码与版本号，
          并内置多个服务器地址依次尝试。安装过程只弹一次 UAC；之后升级、卸载、远程控制都不会再弹。
        </p>

        {install?.enabled ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              {install.artifacts.map((artifact) =>
                artifact.url ? (
                  <a
                    key={artifact.kind}
                    href={artifact.url}
                    download={artifact.filename}
                    className={cn(
                      buttonVariants({ variant: artifact.kind === "cmd" ? "default" : "outline", size: "sm" }),
                      "no-underline",
                    )}
                    title={artifact.hint}
                  >
                    <Download className="size-3.5" />
                    {artifact.label}
                  </a>
                ) : null,
              )}
              {install.artifacts[0]?.url ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => copyInstallLink(install.artifacts[0].url, "install-link")}
                >
                  {copied === "install-link" ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                  复制下载链接
                </Button>
              ) : null}
            </div>

            <dl className="mt-2.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[0.68rem]">
              <dt className="text-faint">版本</dt>
              <dd className="font-mono text-soft">v{install.version}</dd>
              <dt className="text-faint">线路</dt>
              <dd className="break-all text-soft">{install.base_urls.join("  |  ") || "—"}</dd>
              <dt className="text-faint">注册码</dt>
              <dd className="flex items-center gap-1.5 font-mono text-soft">
                <span>{install.enroll_code}</span>
                <Button
                  size="icon-sm"
                  variant="ghost"
                  aria-label="复制注册码"
                  onClick={() => void copy(install.enroll_code, "code")}
                >
                  {copied === "code" ? <Check className="size-3" /> : <Copy className="size-3" />}
                </Button>
              </dd>
            </dl>
          </>
        ) : (
          <p className="rounded-md border border-warn/30 bg-warn/10 px-2.5 py-1.5 text-[0.68rem] leading-relaxed text-warn">
            服务器没有开启注册（<span className="font-mono">OBS_ENROLL_CODE</span> 为空），因此不提供安装程序。
            在服务端环境变量里设置注册码后重启即可。
          </p>
        )}
      </section>
    </div>
  );
}