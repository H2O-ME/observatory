import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, Download, KeyRound, Loader2, RotateCcw, Save, ShieldCheck, Trash2 } from "lucide-react";

import { Banner, ErrorBlock, LoadingBlock } from "@/components/async-state";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { PageShell } from "@/components/page-shell";
import { W2Notice } from "@/components/w2-notice";
import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { api, isApiError } from "@/lib/api";
import { formatBytes, formatDateTime, formatDuration } from "@/lib/format";
import type { InstallToken, SettingSpec } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Ordered sections; unknown keys fall into 其他 so a new server spec still renders. */
const GROUPS: Array<{ title: string; keys: string[] }> = [
  {
    title: "采集",
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
  { title: "连接与传输", keys: ["ws_enabled", "poll_interval_s", "transport_reprobe_s", "heartbeat_interval_s"] },
  { title: "实时控制", keys: ["control_enabled", "control_fps", "control_quality"] },
  { title: "文件管理", keys: ["file_write_enabled", "file_upload_max_mb"] },
  { title: "更新", keys: ["auto_update", "update_channel"] },
  { title: "保留与配额", keys: ["frame_retention_days", "audit_retention_days", "command_retention_days", "storage_quota_mb", "quota_target"] },
  { title: "归档", keys: ["archive_enabled", "archive_repo", "archive_branch", "archive_path_prefix", "archive_token"] },
  { title: "安装", keys: ["install_token_ttl_minutes", "install_script_note"] },
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
 */
export function SettingsPage() {
  const queryClient = useQueryClient();

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
      <div className="space-y-3">
        <LoadingBlock label="正在读取设置…" rows={6} />
      </div>
    );
  }

  if (query.isError) {
    return (
      <ErrorBlock
        message={isApiError(query.error) ? query.error.message : "无法读取设置"}
        retryable
        onRetry={() => void query.refetch()}
      />
    );
  }

  const specs = query.data?.specs ?? {};
  const grouped = new Set(GROUPS.flatMap((group) => group.keys));
  const leftovers = Object.keys(specs).filter((key) => !grouped.has(key));
  const system = systemQuery.data;
  const install = installQuery.data;

  return (
    <PageShell
      title="设置"
      subtitle={`服务端 v${system?.version ?? query.data?.server_version ?? "—"} · 修改只在点“保存”后生效并推送给在线受控端`}
      actions={
        <>
          <Button size="sm" variant="outline" onClick={reset} disabled={saving || (dirtyKeys.length === 0 && secretKeys.length === 0)}>
            <RotateCcw className="size-3.5" />
            放弃修改
          </Button>
          <Button
            onClick={() => void save()}
            disabled={saving || (dirtyKeys.length === 0 && secretKeys.length === 0)}
          >
            {saving ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}
            保存{dirtyKeys.length + secretKeys.length > 0 ? `（${dirtyKeys.length + secretKeys.length} 项）` : ""}
          </Button>
        </>
      }
    >

      {message ? <Banner tone="ok">{message}</Banner> : null}
      {error ? <Banner tone="error">{error}</Banner> : null}
      {dirtyKeys.length + secretKeys.length > 0 ? (
        <Banner tone="warn">
          有 {dirtyKeys.length + secretKeys.length} 项未保存的修改；后台刷新不会覆盖你的编辑（草稿只在本页内）。
        </Banner>
      ) : null}

      {GROUPS.map((group) => {
        const keys = group.keys.filter((key) => specs[key]);
        if (keys.length === 0) return null;
        return (
          <section key={group.title} className="rounded-panel border border-line bg-panel p-3">
            <h2 className="mb-2 text-sm font-semibold text-soft">{group.title}</h2>
            <div className="space-y-2.5">
              {keys.map((key) => (
                <SettingField
                  key={key}
                  name={key}
                  spec={specs[key]}
                  value={draft?.[key]}
                  secretValue={secrets[key] ?? ""}
                  onChange={(value) => update(key, value)}
                  onSecretChange={(value) => setSecrets((current) => ({ ...current, [key]: value }))}
                />
              ))}
            </div>
          </section>
        );
      })}

      {leftovers.length > 0 ? (
        <section className="rounded-panel border border-line bg-panel p-4">
          <h2 className="mb-2 text-sm font-semibold text-soft">其他</h2>
          <div className="space-y-2.5">
            {leftovers.map((key) => (
              <SettingField
                key={key}
                name={key}
                spec={specs[key]}
                value={draft?.[key]}
                secretValue={secrets[key] ?? ""}
                onChange={(value) => update(key, value)}
                onSecretChange={(value) => setSecrets((current) => ({ ...current, [key]: value }))}
              />
            ))}
          </div>
        </section>
      ) : null}

      <section className="rounded-panel border border-line bg-panel p-4">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <h2 className="flex items-center gap-1.5 text-sm font-semibold text-soft">
            <ShieldCheck className="size-3.5" />
            安装令牌
          </h2>
          <span className="text-[0.7rem] text-faint">
            可用 {install?.available_tokens ?? 0} 个 · 默认有效期 {install?.token_ttl_minutes ?? "—"} 分钟
          </span>
        </div>
        <p className="mb-2 text-[0.7rem] leading-relaxed text-faint">
          令牌只用于一次安装，绑定第一台使用它的机器，下载脚本本身不会消耗它。旧的共享注册码已于 0.3.1 移除。
        </p>

        <div className="flex flex-wrap items-end gap-2">
          <div className="space-y-1">
            <Label className="text-[0.7rem] text-faint">有效期</Label>
            <Select value={String(ttl)} onValueChange={(value) => setTtl(Number(value) || 1800)}>
              <SelectTrigger size="sm" className="w-28">
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
          <Button size="sm" onClick={() => createToken.mutate()} disabled={createToken.isPending}>
            <KeyRound className="size-3.5" />
            生成令牌
          </Button>
          {created ? (
            <span className="text-[0.7rem] text-soft">
              最近生成的令牌有效至 {formatDateTime(created.expires)}
            </span>
          ) : null}
        </div>

        {created ? (
          <div className="mt-2 space-y-1.5 rounded-md border border-brand/30 bg-brand/10 p-2.5">
            <p className="text-xs font-medium text-brand">令牌只显示这一次，请立即复制并保存</p>
            <div className="flex items-center gap-1.5">
              <code className="min-w-0 flex-1 truncate rounded border border-line bg-panel px-2 py-1 font-mono text-[0.7rem]">
                {created.token}
              </code>
              <Button size="icon-sm" variant="ghost" aria-label="复制令牌" onClick={() => void copy(created.token, "token")}>
                {copied === "token" ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
              </Button>
            </div>
            <div className="flex flex-wrap gap-1.5">
              <a
                href={created.cmd}
                download={`Observatory-Install-${install?.version ?? ""}.cmd`}
                className={cn(buttonVariants({ variant: "default", size: "sm" }), "no-underline")}
              >
                <Download className="size-3.5" />
                下载安装程序 (.cmd)
              </a>
              <a
                href={created.ps1}
                download={`Observatory-Install-${install?.version ?? ""}.ps1`}
                className={cn(buttonVariants({ variant: "outline", size: "sm" }), "no-underline")}
              >
                <Download className="size-3.5" />
                下载安装脚本 (.ps1)
              </a>
              <Button size="sm" variant="ghost" onClick={() => void copy(absolute(created.cmd), "cmd")}>
                {copied === "cmd" ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                复制下载链接
              </Button>
            </div>
          </div>
        ) : null}

        {tokensQuery.isError ? (
          <ErrorBlock
            message={isApiError(tokensQuery.error) ? tokensQuery.error.message : "无法读取令牌列表"}
            retryable
            onRetry={() => void tokensQuery.refetch()}
          />
        ) : tokensQuery.isLoading ? (
          <LoadingBlock label="正在读取令牌…" rows={2} />
        ) : (tokensQuery.data?.tokens.length ?? 0) === 0 ? (
          <p className="mt-2 text-xs text-soft">还没有令牌。</p>
        ) : (
          <ul className="mt-2 divide-y divide-line rounded-md border border-line">
            {(tokensQuery.data?.tokens ?? []).map((token) => {
              const state = token.used ? "已使用" : token.expired ? "已过期" : "可用";
              return (
                <li key={token.id} className="flex flex-wrap items-center gap-2 px-2 py-1.5 text-[0.7rem]">
                  <span
                    className={cn(
                      "rounded border px-1.5 py-0.5 text-[0.7rem]",
                      state === "可用"
                        ? "border-ok/30 bg-ok/10 text-ok"
                        : state === "已使用"
                          ? "border-line bg-sunken text-faint"
                          : "border-warn/40 bg-warn/10 text-warn",
                    )}
                  >
                    {state}
                  </span>
                  <span className="font-mono text-soft">#{token.id}</span>
                  <span className="text-faint">由 {token.created_by || "console"} 创建</span>
                  <span className="text-faint">有效至 {formatDateTime(token.expires_at_ms)}</span>
                  {token.used_at_ms ? <span className="text-faint">使用于 {formatDateTime(token.used_at_ms)}</span> : null}
                  <Button
                    size="sm"
                    variant="ghost"
                    className="ml-auto"
                    disabled={revokeToken.isPending}
                    onClick={() => setPendingToken(token)}
                  >
                    <Trash2 className="size-3.5 text-danger" />
                    撤销
                  </Button>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <W2Notice
        title="账户与二次验证（W3 补齐）"
        description="登录密码修改、TOTP 绑定/解绑与二维码展示在 W3 波次恢复；当前登录页已支持输入二次验证码，服务端也接受已绑定的 TOTP。"
        items={[
          "修改密码：需要当前密码 + 新密码，服务端校验强度",
          "TOTP 绑定：展示 otpauth:// 二维码与一次性恢复码",
          "登出全部会话：当前实现为清除本浏览器会话",
        ]}
        testId="settings-account-w3"
      />

      <section className="rounded-panel border border-line bg-panel p-4">
        <h2 className="mb-2 text-sm font-semibold text-soft">服务器</h2>
        {systemQuery.isError ? (
          <ErrorBlock message="无法读取服务器状态" retryable onRetry={() => void systemQuery.refetch()} />
        ) : (
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
            <dt className="text-faint">版本</dt>
            <dd className="font-mono text-soft">v{system?.version ?? "—"}</dd>
            <dt className="text-faint">运行时长</dt>
            <dd className="text-soft">{formatDuration(system?.uptime_s)}</dd>
            <dt className="text-faint">设备 / 通道</dt>
            <dd className="text-soft">
              {system?.devices ?? "—"} 台（WS {system?.channels.ws ?? "—"} / 轮询 {system?.channels.poll ?? "—"}）·
              控制会话 {system?.control_sessions ?? "—"} · 待处理命令 {system?.pending_commands ?? "—"}
            </dd>
            <dt className="text-faint">存储</dt>
            <dd className="text-soft">
              画面 {formatBytes(system?.storage.frames_bytes)} · 缩略图 {formatBytes(system?.storage.thumbs_bytes)} · 数据库{" "}
              {formatBytes(system?.storage.db_bytes)} · 磁盘剩余 {formatBytes(system?.storage.disk_free)}
            </dd>
            <dt className="text-faint">域名</dt>
            <dd className="break-all text-soft">{(system?.base_urls ?? []).join("  |  ") || "—"}</dd>
            <dt className="text-faint">归档</dt>
            <dd className="text-soft">
              {system?.install.enabled ? "安装可用" : "安装未启用"} · 画面 {system?.frames ?? 0} 帧 · 命令记录{" "}
              {system?.commands ?? 0} 条
            </dd>
          </dl>
        )}
      </section>

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
    </PageShell>
  );
}

function SettingField({
  name,
  spec,
  value,
  secretValue,
  onChange,
  onSecretChange,
}: {
  name: string;
  spec: SettingSpec;
  value: unknown;
  secretValue: string;
  onChange: (value: unknown) => void;
  onSecretChange: (value: string) => void;
}) {
  const id = `setting-${name}`;
  const hint = spec.per_device ? " · 可按设备覆盖" : "";

  if (spec.secret) {
    return (
      <div className="grid gap-1 sm:grid-cols-[minmax(0,1fr)_minmax(0,16rem)] sm:items-center sm:gap-3">
        <div className="min-w-0">
          <Label htmlFor={id} className="text-xs text-ink">
            {specLabel(name, spec)}
          </Label>
          <p className="text-[0.7rem] text-faint">
            <span className="font-mono">{name}</span> · 已配置的值不会回显，留空表示不修改
          </p>
        </div>
        <Input
          id={id}
          type="password"
          autoComplete="new-password"
          value={secretValue}
          onChange={(event) => onSecretChange(event.target.value)}
          placeholder="留空不修改"
          className="h-9"
        />
      </div>
    );
  }

  return (
    <div className="grid gap-1 sm:grid-cols-[minmax(0,1fr)_minmax(0,16rem)] sm:items-center sm:gap-3">
      <div className="min-w-0">
        <Label htmlFor={id} className="text-xs text-ink">
          {specLabel(name, spec)}
        </Label>
        <p className="text-[0.7rem] text-faint">
          <span className="font-mono">{name}</span>
          {hint}
          {spec.kind === "int" && (spec.min !== null || spec.max !== null)
            ? ` · 范围 ${spec.min ?? "—"}..${spec.max ?? "—"}`
            : ""}
        </p>
      </div>
      {spec.kind === "bool" ? (
        <div className="flex items-center sm:justify-end">
          <Switch
            id={id}
            checked={Boolean(value)}
            onCheckedChange={(checked) => onChange(checked)}
            aria-label={specLabel(name, spec)}
          />
        </div>
      ) : spec.kind === "choice" ? (
        <Select value={String(value ?? spec.choices[0] ?? "")} onValueChange={(next) => onChange(next)}>
          <SelectTrigger size="sm" className="w-full">
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
          className="h-9"
        />
      ) : (
        <Input
          id={id}
          value={typeof value === "string" ? value : value === undefined || value === null ? "" : String(value)}
          onChange={(event) => onChange(event.target.value)}
          className="h-9"
          placeholder={spec.kind === "optional_str" ? "可留空" : undefined}
        />
      )}
    </div>
  );
}