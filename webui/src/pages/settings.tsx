import { useEffect, useMemo, useState } from "react";
import type { ComponentType, ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Archive,
  Camera,
  Check,
  Copy,
  Download,
  FolderOpen,
  KeyRound,
  Link2,
  MonitorPlay,
  PackageCheck,
  Rocket,
  RotateCcw,
  Save,
  Settings2,
  Timer,
  Trash2,
  Wifi,
} from "lucide-react";

import { Banner, ErrorBlock, LoadingBlock } from "@/components/async-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { MetaChip, PageHeader } from "@/components/page-header";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useViewport } from "@/hooks/use-viewport";
import { api, isApiError } from "@/lib/api";
import { formatBytes, formatDateTime, formatDuration } from "@/lib/format";
import type { InstallToken, SettingSpec } from "@/lib/types";
import { cn } from "@/lib/utils";

type IconType = ComponentType<{ className?: string }>;

/** Ordered sections; unknown keys fall into 其他 so a new server spec still renders. */
const GROUPS: Array<{ id: string; title: string; hint: string; icon: IconType; keys: string[] }> = [
  {
    id: "capture",
    title: "采集",
    hint: "截图频率、画质与去重策略",
    icon: Camera,
    keys: [
      "capture_enabled",
      "capture_interval_s",
      "capture_on_change_only",
      "capture_change_threshold",
      "jpeg_quality",
      "max_width",
      "thumb_width",
      "capture_scope",
    ],
  },
  {
    id: "transport",
    title: "连接与传输",
    hint: "实时通道、轮询回退与心跳",
    icon: Wifi,
    keys: ["ws_enabled", "poll_interval_s", "transport_reprobe_s", "heartbeat_interval_s"],
  },
  {
    id: "control",
    title: "实时控制",
    hint: "远程画面与输入频率",
    icon: MonitorPlay,
    keys: ["control_enabled", "control_fps", "control_quality"],
  },
  {
    id: "files",
    title: "文件管理",
    hint: "文件操作与上传体积上限",
    icon: FolderOpen,
    keys: ["file_write_enabled", "file_upload_max_mb"],
  },
  { id: "updates", title: "更新", hint: "受控端自动升级策略", icon: PackageCheck, keys: ["auto_update", "update_channel"] },
  {
    id: "retention",
    title: "保留与配额",
    hint: "服务端数据保留与磁盘配额",
    icon: Timer,
    keys: [
      "frame_retention_days",
      "audit_retention_days",
      "command_retention_days",
      "storage_quota_mb",
      "quota_target",
    ],
  },
  {
    id: "archive",
    title: "归档",
    hint: "远端仓库与归档令牌（secret 不回显）",
    icon: Archive,
    keys: ["archive_enabled", "archive_repo", "archive_branch", "archive_path_prefix", "archive_token"],
  },
  {
    id: "install",
    title: "安装",
    hint: "安装令牌默认有效期与脚本提示",
    icon: Rocket,
    keys: ["install_token_ttl_minutes", "install_script_note"],
  },
];

const TTL_OPTIONS = [
  { value: 1800, label: "30 分钟" },
  { value: 21600, label: "6 小时" },
  { value: 86400, label: "24 小时" },
];

function specLabel(key: string, spec: SettingSpec): string {
  return spec.description || key;
}

/**
 * Settings.
 *
 * Draft handling is the point of this page:
 *  - the draft is seeded **once**; a background refetch (window focus, socket
 *    notice) never overwrites what the operator is typing;
 *  - saving sends only changed keys, then writes the server's response straight
 *    into the query cache (`setQueryData`) and refreshes only the derived
 *    `system` / `install` queries — no full page reload, no query invalidation of
 *    this form.
 *
 * Secrets (`archive_token`) never reach the browser: their value is absent from
 * `values`, the field renders empty and is only sent when the operator types a
 * new one.
 *
 * Presentation (0.4): one centred column of grouped panels (the old desktop
 * section rail duplicated every group title, so it is gone). Phones/watches get
 * a save bar that floats above the dock, so the primary action never scrolls
 * away.
 */
export function SettingsPage() {
  const queryClient = useQueryClient();
  const viewport = useViewport();
  const watch = viewport === "watch";
  const desktop = viewport === "desktop";

  const [draft, setDraft] = useState<Record<string, unknown> | null>(null);
  const [baseline, setBaseline] = useState<Record<string, unknown> | null>(null);
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [copied, setCopied] = useState("");
  const [ttl, setTtl] = useState(1800);
  const [created, setCreated] = useState<{ token: string; cmd: string; ps1: string; expires: number } | null>(null);
  const [pendingToken, setPendingToken] = useState<InstallToken | null>(null);

  const query = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const systemQuery = useQuery({ queryKey: ["system"], queryFn: api.getSystem, refetchInterval: 30_000 });
  const installQuery = useQuery({ queryKey: ["install"], queryFn: api.getInstall, staleTime: 60_000 });
  const tokensQuery = useQuery({ queryKey: ["install-tokens"], queryFn: api.listInstallTokens });

  useEffect(() => {
    // Seed once. `draft !== null` is the guard that keeps refetches from clobbering edits.
    if (!query.data || draft !== null) return;
    setDraft(query.data.values);
    setBaseline(query.data.values);
  }, [query.data, draft]);

  const dirtyKeys = useMemo(() => {
    if (!draft || !baseline) return [];
    return Object.keys(draft).filter((key) => !Object.is(draft[key], baseline[key]));
  }, [draft, baseline]);

  const secretKeys = useMemo(() => Object.keys(secrets).filter((key) => secrets[key].trim() !== ""), [secrets]);

  const dirtyCount = dirtyKeys.length + secretKeys.length;
  const canSave = !saving && dirtyCount > 0;
  const touch = !desktop;
  const stack = watch ? "space-y-3" : "space-y-5";

  const specs = query.data?.specs ?? {};
  const grouped = new Set(GROUPS.flatMap((group) => group.keys));
  const leftovers = Object.keys(specs).filter((key) => !grouped.has(key));
  const sections = GROUPS.map((group) => ({
    ...group,
    keys: group.keys.filter((key) => specs[key]),
  })).filter((group) => group.keys.length > 0);
  const otherSection =
    leftovers.length > 0
      ? { id: "other", title: "其他", hint: "服务端新增、尚未归类的设置键", icon: Settings2, keys: leftovers }
      : null;
  const fieldSections = otherSection ? [...sections, otherSection] : sections;

  const update = (key: string, value: unknown) => {
    setDraft((current) => (current ? { ...current, [key]: value } : current));
  };

  const reset = () => {
    if (!query.data) return;
    setDraft(query.data.values);
    setBaseline(query.data.values);
    setSecrets({});
    setMessage("已放弃未保存的修改");
    setError("");
  };

  const save = async () => {
    if (!draft || !query.data) return;
    const specs = query.data.specs;
    const values: Record<string, unknown> = {};
    for (const key of dirtyKeys) {
      const spec = specs[key];
      const value = draft[key];
      if (spec?.kind === "int") {
        if (value === "" || value === null || value === undefined) {
          setError(`${specLabel(key, spec)} 不能为空`);
          return;
        }
        const number = Number(value);
        if (!Number.isFinite(number)) {
          setError(`${specLabel(key, spec)} 必须是整数`);
          return;
        }
        values[key] = Math.trunc(number);
      } else {
        values[key] = value;
      }
    }
    for (const key of secretKeys) values[key] = secrets[key].trim();
    if (Object.keys(values).length === 0) {
      setMessage("没有需要保存的修改");
      return;
    }
    setSaving(true);
    setError("");
    setMessage("");
    try {
      const result = await api.patchSettings(values);
      // Local update only: the form is now the source of truth for this page.
      queryClient.setQueryData(["settings"], result);
      setDraft(result.values);
      setBaseline(result.values);
      setSecrets({});
      setMessage(
        `已保存 ${Object.keys(values).length} 项${
          typeof result.pushed_to === "number" ? `，已推送到 ${result.pushed_to} 台在线设备` : ""
        }`,
      );
      void queryClient.invalidateQueries({ queryKey: ["system"] });
      void queryClient.invalidateQueries({ queryKey: ["install"] });
    } catch (failure) {
      setError(isApiError(failure) ? failure.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const createToken = useMutation({
    mutationFn: () => api.createInstallToken(ttl),
    onSuccess: (result) => {
      setCreated({
        token: result.token,
        cmd: result.install_cmd_url,
        ps1: result.install_ps1_url,
        expires: result.expires_at_ms,
      });
      setError("");
      void queryClient.invalidateQueries({ queryKey: ["install-tokens"] });
      void queryClient.invalidateQueries({ queryKey: ["install"] });
      void queryClient.invalidateQueries({ queryKey: ["system"] });
    },
    onError: (failure) => setError(isApiError(failure) ? failure.message : "生成失败"),
  });

  const revokeToken = useMutation({
    mutationFn: (id: number) => api.deleteInstallToken(id),
    onSuccess: (result) => {
      setMessage(result.message);
      setPendingToken(null);
      void queryClient.invalidateQueries({ queryKey: ["install-tokens"] });
      void queryClient.invalidateQueries({ queryKey: ["install"] });
      void queryClient.invalidateQueries({ queryKey: ["system"] });
    },
    onError: (failure) => {
      setError(isApiError(failure) ? failure.message : "撤销失败");
      setPendingToken(null);
    },
  });

  const copy = async (text: string, tag: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(tag);
      window.setTimeout(() => setCopied(""), 2000);
    } catch {
      setError("复制失败，请手动选择文本");
    }
  };

  const absolute = (url: string) => (url.startsWith("http") ? url : `${location.origin}${url}`);

  if (query.isLoading) {
    return (
      <div className="w-full min-w-0">
        <PageHeader
          title="设置"
          description="修改只在点“保存”后生效并推送给在线受控端；草稿只保存在本页。"
        />
        <div className="mt-4">
          <LoadingBlock label="正在读取设置…" rows={6} />
        </div>
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className="w-full min-w-0">
        <PageHeader title="设置" description="服务端全局配置" />
        <div className="mt-4">
          <ErrorBlock
            message={isApiError(query.error) ? query.error.message : "无法读取设置"}
            retryable
            onRetry={() => void query.refetch()}
          />
        </div>
      </div>
    );
  }

  const system = systemQuery.data;
  const install = installQuery.data;
  const serverVersion = system?.version ?? query.data?.server_version ?? "—";
  const tokenTotal = tokensQuery.data?.tokens.length ?? 0;

  return (
    <div className={cn("w-full min-w-0", !desktop && "pb-32")}>
      <PageHeader
        title="设置"
        description="修改只在点“保存”后生效并推送给在线受控端；草稿只保存在本页。"
        actions={
          <>
            <Button size="sm" variant="outline" className="h-9 px-3" onClick={reset} disabled={!canSave}>
              <RotateCcw className="size-3.5" />
              放弃
            </Button>
            <Button size="sm" className="h-9 px-3" disabled={!canSave} onClick={() => void save()}>
              {saving ? <Spinner className="size-4" /> : <Save className="size-4" />}
              保存{dirtyCount > 0 ? `（${dirtyCount}）` : ""}
            </Button>
          </>
        }
      />

      <div className={cn("mt-4 min-w-0 sm:mt-5", stack)}>
        {message ? <Banner tone="ok">{message}</Banner> : null}
        {error ? (
          <Banner tone="error" title="操作失败">
            {error}
          </Banner>
        ) : null}

        <div className={cn("w-full min-w-0", stack)}>
            {fieldSections.map((group) => {
              const changed = group.keys.filter(
                (key) => dirtyKeys.includes(key) || (secrets[key] ?? "").trim() !== "",
              ).length;
              return (
                <SettingsCard
                  key={group.id}
                  padded={false}
                  title={group.title}
                  description={group.hint}
                  actions={
                    changed > 0 ? (
                      <MetaChip tone="warn" className="text-xs">
                        {changed} 项改动
                      </MetaChip>
                    ) : undefined
                  }
                >
                  <div className="divide-y divide-border">
                    {group.keys.map((key) => (
                      <SettingField
                        key={key}
                        name={key}
                        spec={specs[key]}
                        value={draft?.[key]}
                        secretValue={secrets[key] ?? ""}
                        dirty={dirtyKeys.includes(key) || (secrets[key] ?? "").trim() !== ""}
                        touch={touch}
                        onChange={(value) => update(key, value)}
                        onSecretChange={(value) => setSecrets((current) => ({ ...current, [key]: value }))}
                      />
                    ))}
                  </div>
                </SettingsCard>
              );
            })}

            {/* ------------------------------------------------ install launchpad */}
            <div className="min-w-0">
              <SettingsCard
                title="安装令牌"
                description="令牌只用于一次安装，绑定第一台使用它的机器；下载脚本本身不会消耗它。"
                actions={
                  <>
                    <MetaChip tone="ok">可用 {install?.available_tokens ?? 0}</MetaChip>
                    <MetaChip>默认 {install?.token_ttl_minutes ?? "—"} 分钟</MetaChip>
                  </>
                }
              >
                  <div className="min-w-0">
                    <div className="grid min-w-0 gap-2 sm:grid-cols-[minmax(0,10rem)_auto] sm:items-end sm:gap-3">
                      <div className="min-w-0">
                        <Label className="text-xs text-faint">有效期</Label>
                        <Select value={String(ttl)} onValueChange={(value) => setTtl(Number(value) || 1800)}>
                          <SelectTrigger size="sm" className={cn("mt-1 w-full", touch && "tap")}>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {TTL_OPTIONS.map((option) => (
                              <SelectItem key={option.value} value={String(option.value)}>
                                {option.label}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="min-w-0">
                        <Button disabled={createToken.isPending} onClick={() => createToken.mutate()}>
                          {createToken.isPending ? <Spinner className="size-4" /> : <KeyRound className="size-4" />}
                          生成令牌
                        </Button>
                      </div>
                    </div>

                    {created ? (
                      <div className="mt-3 min-w-0 overflow-hidden rounded-md border border-border bg-muted p-3">
                        <div className="min-w-0">
                          <div className="min-w-0">
                            <p className="text-sm font-semibold text-primary">令牌只显示这一次</p>
                            <p className="text-xs leading-relaxed text-muted-foreground">
                              有效至 <span className="tnum">{formatDateTime(created.expires)}</span>；请立即复制或下载安装脚本。
                            </p>
                          </div>

                          <div className="mt-2 grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-1.5">
                            <code className="min-w-0 break-all rounded-md border border-border bg-card px-2.5 py-2 font-mono text-xs leading-relaxed text-foreground">
                              {created.token}
                            </code>
                            <Button
                              size="icon-sm"
                              variant="outline"
                              className={cn("shrink-0", !desktop && "size-11")}
                              aria-label="复制令牌"
                              title="复制令牌"
                              onClick={() => void copy(created.token, "token")}
                            >
                              {copied === "token" ? <Check className="size-4" /> : <Copy className="size-4" />}
                            </Button>
                          </div>

                          <div className="mt-2 grid min-w-0 gap-1.5 sm:grid-cols-2">
                            <a
                              href={created.cmd}
                              download={`Observatory-Install-${install?.version ?? ""}.cmd`}
                              className={cn(
                                buttonVariants({ variant: "default", size: "sm" }),
                                "h-11 min-w-0 justify-center no-underline sm:h-9",
                              )}
                            >
                              <Download className="mr-1.5 size-4 shrink-0" />
                              <span className="truncate">下载安装程序 (.cmd)</span>
                            </a>
                            <a
                              href={created.ps1}
                              download={`Observatory-Install-${install?.version ?? ""}.ps1`}
                              className={cn(
                                buttonVariants({ variant: "outline", size: "sm" }),
                                "h-11 min-w-0 justify-center no-underline sm:h-9",
                              )}
                            >
                              <Download className="mr-1.5 size-4 shrink-0" />
                              <span className="truncate">下载安装脚本 (.ps1)</span>
                            </a>
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-11 justify-center sm:col-span-2 sm:h-9"
                              onClick={() => void copy(absolute(created.cmd), "cmd")}
                            >
                              {copied === "cmd" ? (
                                <Check className="mr-1.5 size-4" />
                              ) : (
                                <Link2 className="mr-1.5 size-4" />
                              )}
                              复制下载链接
                            </Button>
                          </div>
                        </div>
                      </div>
                    ) : null}

                    {tokensQuery.isError ? (
                      <div className="mt-3">
                        <ErrorBlock
                          message={isApiError(tokensQuery.error) ? tokensQuery.error.message : "无法读取令牌列表"}
                          retryable
                          onRetry={() => void tokensQuery.refetch()}
                        />
                      </div>
                    ) : tokensQuery.isLoading ? (
                      <div className="mt-3">
                        <LoadingBlock label="正在读取令牌…" rows={2} />
                      </div>
                    ) : tokenTotal === 0 ? (
                      <div className="mt-3 min-w-0 overflow-hidden rounded-md border border-dashed border-border bg-muted px-3 py-6 text-center">
                        <div>
                          <KeyRound className="mx-auto size-5 text-faint" />
                          <p className="mt-2 text-sm font-medium text-foreground">还没有可用的安装令牌</p>
                          <p className="mx-auto mt-1 max-w-sm text-xs leading-relaxed text-muted-foreground">
                            生成一枚令牌后即可下载安装脚本；脚本下载不会消耗令牌，首次安装时才会绑定设备。
                          </p>
                          <Button
                            size="sm"
                            variant="outline"
                            className="mt-3 h-11 sm:h-9"
                            onClick={() => createToken.mutate()}
                            disabled={createToken.isPending}
                          >
                            <KeyRound className="size-3.5" />
                            生成第一枚令牌
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <ul className="mt-3 grid min-w-0 gap-1.5">
                        {(tokensQuery.data?.tokens ?? []).map((token) => {
                          const state = token.used ? "已使用" : token.expired ? "已过期" : "可用";
                          return (
                            <li
                              key={token.id}
                              className="grid min-w-0 gap-2 rounded-md border border-border bg-card p-2.5 sm:grid-cols-[auto_minmax(0,1fr)_auto] sm:items-center sm:gap-3"
                            >
                              <Badge
                                variant={state === "可用" ? "success" : state === "已使用" ? "neutral" : "warning"}
                                className="w-fit text-xs font-semibold"
                              >
                                {state}
                              </Badge>
                              <div className="min-w-0">
                                <p className="min-w-0 break-all text-xs text-foreground">
                                  <span className="tnum font-mono text-muted-foreground">#{token.id}</span>
                                  <span className="ml-2 text-faint">由 {token.created_by || "console"} 创建</span>
                                </p>
                                <p className="mt-0.5 min-w-0 break-all text-xs leading-relaxed text-faint">
                                  有效至 {formatDateTime(token.expires_at_ms)}
                                  {token.used_at_ms ? ` · 使用于 ${formatDateTime(token.used_at_ms)}` : ""}
                                </p>
                              </div>
                              <Button
                                size="sm"
                                variant="ghost"
                                className="h-11 justify-start sm:justify-center"
                                disabled={revokeToken.isPending}
                                onClick={() => setPendingToken(token)}
                              >
                                <Trash2 className="size-3.5 text-destructive" />
                                撤销
                              </Button>
                            </li>
                          );
                        })}
                      </ul>
                    )}
                  </div>
              </SettingsCard>
            </div>

            {/* ------------------------------------------------ account hand-off */}
            <SettingsCard
              title="账户与安全"
              description="密码修改、二次验证与会话登出在独立页面维护"
              actions={
                <Link
                  to="/account"
                  className={cn(buttonVariants({ variant: "outline", size: "sm" }), "h-11 no-underline sm:h-9")}
                >
                  打开账户页
                </Link>
              }
            >
              <p className="text-sm leading-relaxed text-muted-foreground">
                登录密码、二次验证（TOTP）与会话登出都在账户页完成，本页不再重复这些表单。
              </p>
            </SettingsCard>

            {/* ------------------------------------------------ server summary */}
            <SettingsCard
              title="服务器"
              description="只读状态"
              actions={
                systemQuery.isError ? (
                  <MetaChip tone="danger">读取失败</MetaChip>
                ) : (
                  <MetaChip tone="brand">v{serverVersion}</MetaChip>
                )
              }
            >
                {systemQuery.isError ? (
                  <ErrorBlock message="无法读取服务器状态" retryable onRetry={() => void systemQuery.refetch()} />
                ) : (
                  <dl className="grid min-w-0 grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2">
                    <ServerRow label="版本" value={`v${system?.version ?? "—"}`} mono />
                    <ServerRow label="运行时长" value={formatDuration(system?.uptime_s)} />
                    <ServerRow
                      label="设备 / 通道"
                      value={`${system?.devices ?? "—"} 台（WS ${system?.channels.ws ?? "—"} / 轮询 ${system?.channels.poll ?? "—"}）`}
                    />
                    <ServerRow
                      label="控制会话 / 待处理命令"
                      value={`${system?.control_sessions ?? "—"} / ${system?.pending_commands ?? "—"}`}
                      mono
                    />
                    <ServerRow
                      label="存储"
                      value={`画面 ${formatBytes(system?.storage.frames_bytes)} · 缩略图 ${formatBytes(
                        system?.storage.thumbs_bytes,
                      )} · 数据库 ${formatBytes(system?.storage.db_bytes)} · 磁盘剩余 ${formatBytes(system?.storage.disk_free)}`}
                    />
                    <ServerRow
                      label="安装 / 数据量"
                      value={`${system?.install.enabled ? "安装可用" : "安装未启用"} · 画面 ${system?.frames ?? 0} 帧 · 命令记录 ${
                        system?.commands ?? 0
                      } 条`}
                    />
                    <ServerRow
                      label="域名"
                      value={(system?.base_urls ?? []).join("  |  ") || "—"}
                      className="sm:col-span-2"
                    />
                  </dl>
                )}
            </SettingsCard>
        </div>
      </div>



      <ConfirmDialog
        open={pendingToken !== null}
        onOpenChange={(open) => {
          if (!open) setPendingToken(null);
        }}
        title="撤销安装令牌"
        description={
          pendingToken
            ? `撤销令牌 #${pendingToken.id}？已下载但尚未使用的安装脚本将立即失效（已安装完成的设备不受影响）。`
            : ""
        }
        confirmLabel="撤销"
        busy={revokeToken.isPending}
        onConfirm={() => {
          if (pendingToken) revokeToken.mutate(pendingToken.id);
        }}
      />
    </div>
  );
}

/** One label/value pair in the read-only server card. */
function ServerRow({
  label,
  value,
  mono = false,
  className,
}: {
  label: string;
  value: string;
  mono?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("flex min-w-0 items-baseline justify-between gap-4", className)}>
      <dt className="shrink-0 text-sm text-foreground">{label}</dt>
      <dd className={cn("min-w-0 break-all text-right text-sm text-muted-foreground", mono && "tnum")}>{value}</dd>
    </div>
  );
}

/**
 * Settings group: one flat surface with a shadow instead of a border, a 16px
 * title, a 14px description and a divider before the rows. Cards breathe at
 * p-6 so content never touches the edge.
 */
function SettingsCard({
  title,
  description,
  actions,
  children,
  padded = true,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  padded?: boolean;
}) {
  return (
    <section className="min-w-0 overflow-hidden rounded-2xl bg-card shadow-sm">
      <header className="flex min-w-0 items-start justify-between gap-3 px-6 pt-5 pb-4">
        <div className="min-w-0">
          <h2 className="text-base font-semibold tracking-[-0.01em] text-foreground">{title}</h2>
          {description ? (
            <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{description}</p>
          ) : null}
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-1.5">{actions}</div> : null}
      </header>
      <div className={cn("border-t border-border", padded && "px-6 py-5")}>{children}</div>
    </section>
  );
}

/**
 * One settings row: label/hint on the left, control on the right on wide
 * screens; stacked on phones and watches (no fixed-width control column).
 */
function SettingField({
  name,
  spec,
  value,
  secretValue,
  dirty,
  touch,
  onChange,
  onSecretChange,
}: {
  name: string;
  spec: SettingSpec;
  value: unknown;
  secretValue: string;
  dirty: boolean;
  touch: boolean;
  onChange: (value: unknown) => void;
  onSecretChange: (value: string) => void;
}) {
  const id = `setting-${name}`;
  const label = specLabel(name, spec);
  const range =
    spec.kind === "int" && (spec.min !== null || spec.max !== null)
      ? `范围 ${spec.min ?? "—"}..${spec.max ?? "—"}`
      : "";
  const height = touch ? "h-11" : "h-9";

  return (
    <div className="grid min-w-0 gap-3 px-6 py-4 sm:grid-cols-[minmax(0,1fr)_16rem] sm:items-center sm:gap-6">
      <div className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <Label htmlFor={id} className="min-w-0 text-sm font-medium leading-relaxed text-foreground">
            {label}
          </Label>
          {dirty ? (
            <Badge variant="warning" className="text-xs font-medium">
              已修改
            </Badge>
          ) : null}
        </div>
        <div className="mt-2 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs leading-relaxed text-muted-foreground">
          <code className="rounded-sm bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
            {name}
          </code>
          <span>{spec.per_device ? "可按设备覆盖" : "仅全局"}</span>
          {spec.secret ? <span>已配置值不回显，留空表示不修改</span> : null}
          {range ? <span>{range}</span> : null}
        </div>
      </div>

      <div className="flex min-w-0 items-center justify-end">
        {spec.secret ? (
          <Input
            id={id}
            type="password"
            autoComplete="new-password"
            value={secretValue}
            onChange={(event) => onSecretChange(event.target.value)}
            placeholder="留空不修改"
            className={cn("w-full", height)}
          />
        ) : spec.kind === "bool" ? (
          <Switch
            id={id}
            checked={Boolean(value)}
            onCheckedChange={(checked) => onChange(checked)}
            aria-label={label}
          />
        ) : spec.kind === "choice" ? (
          <Select value={String(value ?? spec.choices[0] ?? "")} onValueChange={(next) => onChange(next)}>
            <SelectTrigger id={id} className="w-48">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {spec.choices.map((choice) => (
                <SelectItem key={choice} value={choice}>
                  {choice}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : spec.kind === "int" ? (
          <Input
            id={id}
            type="number"
            inputMode="numeric"
            value={value === undefined || value === null ? "" : String(value)}
            onChange={(event) => onChange(event.target.value === "" ? "" : Number(event.target.value))}
            className={cn("tnum w-32 text-right", height)}
          />
        ) : (
          <Input
            id={id}
            value={typeof value === "string" ? value : value === undefined || value === null ? "" : String(value)}
            onChange={(event) => onChange(event.target.value)}
            className={cn("w-full", height)}
            placeholder={spec.kind === "optional_str" ? "可留空" : undefined}
          />
        )}
      </div>
    </div>
  );
}
