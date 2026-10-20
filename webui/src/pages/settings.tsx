import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Check, Copy, Download, KeyRound, Save, ShieldCheck, ShieldOff } from "lucide-react";

import { Button, buttonVariants } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { api, ApiError } from "@/lib/api";
import { formatBytes, formatDuration } from "@/lib/format";
import type { SettingSpec, SettingsPayload } from "@/lib/types";
import { cn } from "@/lib/utils";

const GROUPS: Array<{ title: string; hint?: string; keys: string[] }> = [
  {
    title: "画面采集",
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
    title: "连接",
    hint: "受控端优先使用 WebSocket，失败时自动回退到轮询，并定期尝试重新升级。",
    keys: ["ws_enabled", "poll_interval_s", "transport_reprobe_s", "heartbeat_interval_s"],
  },
  {
    title: "实时控制",
    keys: ["control_enabled", "control_fps", "control_quality"],
  },
  {
    title: "文件管理",
    keys: ["file_write_enabled", "file_upload_max_mb"],
  },
  {
    title: "自动更新",
    hint: "受控端会在心跳中拿到新版本信息，自行下载校验并替换，无需重新安装。",
    keys: ["auto_update", "update_channel"],
  },
  {
    title: "数据保留",
    keys: ["frame_retention_days", "audit_retention_days", "command_retention_days", "storage_quota_mb", "quota_target"],
  },
  {
    title: "归档（可选）",
    hint: "开启后每张画面会额外提交到私有 GitHub 仓库，作为异地副本。",
    keys: ["archive_enabled", "archive_repo", "archive_token", "archive_branch", "archive_path_prefix", "archive_cache_max_mb"],
  },
  {
    title: "其他",
    keys: ["install_script_note"],
  },
];

function fieldValue(values: Record<string, unknown>, key: string): unknown {
  return values[key];
}

function ToggleRow({
  label,
  description,
  checked,
  onChange,
}: {
  label: string;
  description: string;
  checked: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <div className="flex min-h-11 items-center justify-between gap-3 py-1">
      <div className="min-w-0">
        <p className="text-[0.78rem] font-medium text-ink">{label}</p>
        <p className="text-[0.66rem] leading-relaxed text-faint">{description}</p>
      </div>
      <Switch checked={checked} onCheckedChange={(value) => onChange(Boolean(value))} />
    </div>
  );
}

function SettingField({
  name,
  spec,
  value,
  onChange,
}: {
  name: string;
  spec: SettingSpec;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const label = spec.description || name;

  if (spec.kind === "bool") {
    return <ToggleRow label={label} description={name} checked={Boolean(value)} onChange={onChange} />;
  }

  if (spec.kind === "choice") {
    return (
      <div className="flex min-h-11 items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[0.78rem] font-medium text-ink">{label}</p>
          <p className="text-[0.66rem] text-faint">{name}</p>
        </div>
        <Select value={String(value ?? "")} onValueChange={(next) => onChange(String(next))}>
          <SelectTrigger className="h-8 w-36">
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
      </div>
    );
  }

  if (spec.kind === "int") {
    return (
      <div className="flex min-h-11 items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[0.78rem] font-medium text-ink">{label}</p>
          <p className="text-[0.66rem] text-faint">
            {name}
            {spec.min !== null && spec.max !== null ? ` · ${spec.min}–${spec.max}` : ""}
          </p>
        </div>
        <Input
          type="number"
          min={spec.min ?? undefined}
          max={spec.max ?? undefined}
          value={value === undefined || value === null ? "" : String(value)}
          onChange={(event) => onChange(Number(event.target.value))}
          className="h-8 w-28 text-right"
        />
      </div>
    );
  }

  return (
    <div className="space-y-1 py-1">
      <Label className="text-[0.78rem] font-medium text-ink">{label}</Label>
      <Input
        type={spec.secret ? "password" : "text"}
        value={value === undefined || value === null ? "" : String(value)}
        onChange={(event) => onChange(event.target.value)}
        placeholder={spec.secret ? "已保存（留空表示不修改）" : name}
        className="h-8 text-xs"
      />
      <p className="text-[0.62rem] text-faint">{name}</p>
    </div>
  );
}

export function SettingsPage() {
  const settingsQuery = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const systemQuery = useQuery({ queryKey: ["system"], queryFn: api.system, refetchInterval: 30_000 });
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [copied, setCopied] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [totpSecret, setTotpSecret] = useState("");

  const data: SettingsPayload | undefined = settingsQuery.data;
  const system = systemQuery.data;

  useEffect(() => {
    if (data) setDraft(data.values);
  }, [data]);

  const dirty = useMemo(() => {
    if (!data) return false;
    return Object.keys(draft).some((key) => JSON.stringify(draft[key]) !== JSON.stringify(data.values[key]));
  }, [data, draft]);

  const update = (key: string, value: unknown) => setDraft((previous) => ({ ...previous, [key]: value }));

  const save = async () => {
    if (!data) return;
    setSaving(true);
    setError("");
    setMessage("");
    try {
      const changed: Record<string, unknown> = {};
      for (const key of Object.keys(draft)) {
        if (JSON.stringify(draft[key]) !== JSON.stringify(data.values[key])) changed[key] = draft[key];
      }
      if (Object.keys(changed).length === 0) {
        setMessage("没有需要保存的修改");
        return;
      }
      await api.patchSettings(changed);
      setMessage(`已保存 ${Object.keys(changed).length} 项设置，在线受控端会立即收到`);
      await settingsQuery.refetch();
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.detail : "保存失败");
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

  const install = system?.install;
  const installArtifacts = install?.artifacts ?? [];

  const copyInstallLink = (url: string, tag: string) => {
    const absolute = url.startsWith("http") ? url : `${location.origin}${url}`;
    void copy(absolute, tag);
  };

  const changePassword = async () => {
    setError("");
    setMessage("");
    try {
      const result = await api.changePassword(currentPassword, newPassword);
      setMessage(result.message);
      setCurrentPassword("");
      setNewPassword("");
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.detail : "修改密码失败");
    }
  };

  const startTotp = async () => {
    setError("");
    try {
      const result = await api.totpSetup();
      setTotpSecret(result.secret);
      setMessage("请用验证器添加密钥，然后输入当前验证码启用");
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.detail : "无法生成密钥");
    }
  };

  const enableTotp = async () => {
    try {
      const result = await api.totpEnable(totpSecret, totpCode);
      setMessage(result.message);
      setTotpCode("");
      setTotpSecret("");
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.detail : "启用失败");
    }
  };

  const disableTotp = async () => {
    try {
      const result = await api.totpDisable(totpCode);
      setMessage(result.message);
      setTotpCode("");
    } catch (failure) {
      setError(failure instanceof ApiError ? failure.detail : "关闭失败");
    }
  };

  if (settingsQuery.isLoading) {
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
        <Button size="sm" onClick={() => void save()} disabled={!dirty || saving}>
          <Save className="size-3.5" />
          保存修改
        </Button>
      </div>

      {message ? <p className="rounded-md border border-ok/30 bg-ok/10 px-2.5 py-1.5 text-xs text-ok">{message}</p> : null}
      {error ? <p className="rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger">{error}</p> : null}

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-2 text-xs font-semibold text-soft">服务器</h2>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[0.72rem]">
          <dt className="text-faint">版本</dt>
          <dd className="font-mono text-soft">v{system?.version ?? data?.server_version ?? "—"}</dd>
          <dt className="text-faint">运行时长</dt>
          <dd className="text-soft">{formatDuration(system?.uptime_s ?? 0)}</dd>
          <dt className="text-faint">设备</dt>
          <dd className="text-soft">
            {system?.devices ?? 0} 台（WS {system?.channels.ws ?? 0} / 轮询 {system?.channels.poll ?? 0}）
          </dd>
          <dt className="text-faint">存储</dt>
          <dd className="text-soft">
            画面 {formatBytes(system?.storage.frames_bytes ?? 0)} · 数据库 {formatBytes(system?.storage.db_bytes ?? 0)} · 磁盘剩余{" "}
            {formatBytes(system?.storage.disk_free ?? 0)}
          </dd>
          <dt className="text-faint">域名</dt>
          <dd className="break-all text-soft">{(system?.base_urls ?? []).join("  |  ")}</dd>
        </dl>
      </section>

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-1 text-xs font-semibold text-soft">安装程序</h2>
        <p className="mb-2 text-[0.66rem] leading-relaxed text-faint">
          下载后双击运行即可，<b className="text-soft">不用输任何参数</b>：文件里已经带好注册码和版本号，
          并内置多个服务器地址依次尝试。安装过程只弹一次 UAC；之后升级、卸载、远程控制都不会再弹。
        </p>

        {install?.enabled ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              {installArtifacts.map((artifact) =>
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
              {installArtifacts[0]?.url ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => copyInstallLink(installArtifacts[0].url, "install-link")}
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
              <dd className="break-all text-soft">{install.base_urls.join("  |  ")}</dd>
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
            <p className="mt-1.5 text-[0.62rem] leading-relaxed text-faint">
              命令行部署（组策略/CI）也可以用 .ps1：
              <span className="font-mono"> powershell -NoProfile -ExecutionPolicy Bypass -File Observatory-Install-{install.version}.ps1</span>
            </p>
          </>
        ) : (
          <p className="rounded-md border border-warn/30 bg-warn/10 px-2.5 py-1.5 text-[0.68rem] leading-relaxed text-warn">
            服务器没有开启注册（<span className="font-mono">OBS_ENROLL_CODE</span> 为空），
            因此不提供安装程序。在服务器 <span className="font-mono">server/.env</span> 里设置注册码后重启容器即可。
          </p>
        )}
      </section>

      {GROUPS.map((group) => {
        const specs = data?.specs ?? {};
        const keys = group.keys.filter((key) => specs[key]);
        if (keys.length === 0) return null;
        return (
          <section key={group.title} className="rounded-panel border border-line bg-panel p-3">
            <h2 className="text-xs font-semibold text-soft">{group.title}</h2>
            {group.hint ? <p className="mb-1 mt-0.5 text-[0.64rem] leading-relaxed text-faint">{group.hint}</p> : null}
            <div className="mt-1 divide-y divide-line/60">
              {keys.map((key) => (
                <SettingField
                  key={key}
                  name={key}
                  spec={specs[key]}
                  value={fieldValue(draft, key)}
                  onChange={(value) => update(key, value)}
                />
              ))}
            </div>
          </section>
        );
      })}

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-soft">
          <KeyRound className="size-3.5" /> 修改密码
        </h2>
        <div className="grid gap-2 sm:grid-cols-2">
          <Input
            type="password"
            placeholder="当前密码"
            value={currentPassword}
            onChange={(event) => setCurrentPassword(event.target.value)}
            className="h-9"
          />
          <Input
            type="password"
            placeholder="新密码（至少 10 位）"
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
            className="h-9"
          />
        </div>
        <Button
          size="sm"
          className="mt-2"
          onClick={() => void changePassword()}
          disabled={!currentPassword || newPassword.length < 10}
        >
          更新密码
        </Button>
      </section>

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-soft">
          <ShieldCheck className="size-3.5" /> 二次验证
        </h2>
        <p className="mb-2 text-[0.66rem] leading-relaxed text-faint">
          关闭二次验证需要输入当前验证码——服务器会校验，不会凭一个空请求就关掉。
        </p>
        {totpSecret ? (
          <div className="space-y-2">
            <p className="break-all rounded bg-sunken p-2 font-mono text-[0.66rem] text-soft">{totpSecret}</p>
            <div className="flex gap-2">
              <Input
                value={totpCode}
                onChange={(event) => setTotpCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
                placeholder="6 位验证码"
                className="h-9 w-32 text-center font-mono"
              />
              <Button size="sm" onClick={() => void enableTotp()} disabled={totpCode.length !== 6}>
                启用
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setTotpSecret("")}>
                取消
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" onClick={() => void startTotp()}>
              <ShieldCheck className="size-3.5" />
              启用二次验证
            </Button>
            <Input
              value={totpCode}
              onChange={(event) => setTotpCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
              placeholder="关闭需输入验证码"
              className="h-9 w-40 text-center font-mono"
            />
            <Button size="sm" variant="destructive" onClick={() => void disableTotp()} disabled={totpCode.length !== 6}>
              <ShieldOff className="size-3.5" />
              关闭
            </Button>
          </div>
        )}
      </section>

      <Separator />
      <p className={cn("pb-2 text-[0.64rem] leading-relaxed text-faint")}>
        Observable 0.2.0 起，版本号在仓库根目录的 VERSION 文件里统一维护，控制台、安装脚本与受控端始终一致。
      </p>
    </div>
  );
}