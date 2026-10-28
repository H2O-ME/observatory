import { useEffect, useMemo, useState } from "react";
import type { ComponentType, ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  Archive,
  Camera,
  Check,
  Download,
  FolderOpen,
  Link2,
  MonitorPlay,
  PackageCheck,
  Rocket,
  RotateCcw,
  Save,
  Settings2,
  Timer,
  Wifi,
} from "lucide-react";

import { Banner, ErrorBlock, LoadingBlock } from "@/components/async-state";
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
import { formatBytes, formatDuration } from "@/lib/format";
import type { SettingSpec } from "@/lib/types";
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
    hint: "安装页备注",
    icon: Rocket,
    keys: ["install_script_note"],
  },
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

  const query = useQuery({ queryKey: ["settings"], queryFn: api.getSettings });
  const systemQuery = useQuery({ queryKey: ["system"], queryFn: api.getSystem, refetchInterval: 30_000 });
  const installQuery = useQuery({ queryKey: ["install"], queryFn: api.getInstall, staleTime: 60_000 });

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
                title="安装受控端"
                description="安装脚本内不含任何凭据：下载即可用，设备首次上线会自动注册（不需要审批、不需要令牌）。删除设备后重新安装同样自动注册。"
                actions={<MetaChip tone="ok">开放注册</MetaChip>}
              >
                <div className="min-w-0">
                  {installQuery.isError ? (
                    <ErrorBlock
                      message={isApiError(installQuery.error) ? installQuery.error.message : "无法读取安装信息"}
                      retryable
                      onRetry={() => void installQuery.refetch()}
                    />
                  ) : (
                    <>
                      <div className="grid min-w-0 gap-1.5 sm:grid-cols-2">
                        <a
                          href={install?.cmd_url ?? "/api/v1/public/install.cmd"}
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
                          href={install?.script_url ?? "/api/v1/public/install.ps1"}
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
                          onClick={() =>
                            void copy(absolute(install?.cmd_url ?? "/api/v1/public/install.cmd"), "cmd")
                          }
                        >
                          {copied === "cmd" ? (
                            <Check className="mr-1.5 size-4" />
                          ) : (
                            <Link2 className="mr-1.5 size-4" />
                          )}
                          复制下载链接
                        </Button>
                      </div>
                      <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
                        版本 {install?.version ?? "—"}；安装时只弹一次 UAC，之后自动更新、远程卸载都不再提示。命令行安装：
                      </p>
                      <code className="mt-1.5 block min-w-0 break-all rounded-md border border-border bg-muted px-2.5 py-2 font-mono text-xs leading-relaxed text-muted-foreground">
                        irm {absolute(install?.script_url ?? "/api/v1/public/install.ps1")} | iex
                      </code>
                    </>
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