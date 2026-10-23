import { useState } from "react";
import { Check, Copy, KeyRound, Loader2, ShieldCheck, ShieldOff } from "lucide-react";

import { Banner, PageHeader } from "@/components/async-state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, isApiError } from "@/lib/api";
import { useAuth } from "@/store/auth";
import { cn } from "@/lib/utils";

/**
 * Account & security.
 *
 * The server owns every rule: the password minimum is enforced server-side
 * (`console/auth.py::change_password`), TOTP enable/disable both require a valid
 * code, and a disable request without a correct code is rejected. The console
 * mirrors the checks for fast feedback but never treats them as authoritative.
 *
 * The TOTP secret is shown as text (secret + `otpauth://` URI): that is the
 * documented fallback for the QR code — no QR image is generated locally, so
 * nothing about the secret leaves the server.
 */
export function AccountPage() {
  const { totpEnabled, version, consoleHost, baseUrls, refresh, logout } = useAuth();

  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [passwordMessage, setPasswordMessage] = useState("");
  const [passwordError, setPasswordError] = useState("");

  const [setup, setSetup] = useState<{ secret: string; uri: string } | null>(null);
  const [setupCode, setSetupCode] = useState("");
  const [disableCode, setDisableCode] = useState("");
  const [totpBusy, setTotpBusy] = useState(false);
  const [totpMessage, setTotpMessage] = useState("");
  const [totpError, setTotpError] = useState("");
  const [copied, setCopied] = useState("");

  const copy = async (text: string, tag: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(tag);
      window.setTimeout(() => setCopied(""), 2000);
    } catch {
      setTotpError("复制失败，请手动选择文本");
    }
  };

  const submitPassword = async (event: React.FormEvent) => {
    event.preventDefault();
    const issue =
      next.length < 10
        ? "新密码至少 10 位"
        : next !== confirm
          ? "两次输入的新密码不一致"
          : next === current
            ? "新密码不能与当前密码相同"
            : "";
    if (issue) {
      setPasswordError(issue);
      return;
    }
    setPasswordBusy(true);
    setPasswordError("");
    setPasswordMessage("");
    try {
      const result = await api.changePassword(current, next);
      setPasswordMessage(result.message || "密码已更新");
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (failure) {
      setPasswordError(isApiError(failure) ? failure.message : "修改密码失败");
    } finally {
      setPasswordBusy(false);
    }
  };

  const startSetup = async () => {
    setTotpBusy(true);
    setTotpError("");
    setTotpMessage("");
    try {
      const result = await api.totpSetup();
      setSetup(result);
      setSetupCode("");
    } catch (failure) {
      setTotpError(isApiError(failure) ? failure.message : "无法生成 TOTP 密钥");
    } finally {
      setTotpBusy(false);
    }
  };

  const enableTotp = async () => {
    if (!setup) return;
    if (!/^\d{6}$/.test(setupCode)) {
      setTotpError("请输入 6 位验证码");
      return;
    }
    setTotpBusy(true);
    setTotpError("");
    setTotpMessage("");
    try {
      const result = await api.totpEnable(setup.secret, setupCode);
      setTotpMessage(result.message || "已开启二次验证");
      setSetup(null);
      setSetupCode("");
      await refresh();
    } catch (failure) {
      setTotpError(isApiError(failure) ? failure.message : "开启失败");
    } finally {
      setTotpBusy(false);
    }
  };

  const disableTotp = async () => {
    if (!/^\d{6}$/.test(disableCode)) {
      setTotpError("请输入当前 6 位验证码");
      return;
    }
    setTotpBusy(true);
    setTotpError("");
    setTotpMessage("");
    try {
      const result = await api.totpDisable(disableCode);
      setTotpMessage(result.message || "已关闭二次验证");
      setDisableCode("");
      await refresh();
    } catch (failure) {
      setTotpError(isApiError(failure) ? failure.message : "关闭失败");
    } finally {
      setTotpBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <PageHeader
        title="账户与安全"
        subtitle="修改控制台密码、管理二次验证（TOTP）。服务端规则：密码至少 10 位；开启/关闭二次验证都需要有效验证码。"
      />

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-soft">
          <KeyRound className="size-3.5" />
          修改密码
        </h2>
        <form className="space-y-2" onSubmit={submitPassword}>
          <div className="grid gap-2 sm:grid-cols-3">
            <div className="space-y-1">
              <Label htmlFor="current-password" className="text-[0.7rem] text-faint">
                当前密码
              </Label>
              <Input
                id="current-password"
                type="password"
                autoComplete="current-password"
                value={current}
                onChange={(event) => setCurrent(event.target.value)}
                className="h-9"
                required
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="new-password" className="text-[0.7rem] text-faint">
                新密码（至少 10 位）
              </Label>
              <Input
                id="new-password"
                type="password"
                autoComplete="new-password"
                value={next}
                onChange={(event) => setNext(event.target.value)}
                className="h-9"
                required
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="confirm-password" className="text-[0.7rem] text-faint">
                确认新密码
              </Label>
              <Input
                id="confirm-password"
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
                className="h-9"
                required
              />
            </div>
          </div>
          {passwordMessage ? <Banner tone="ok">{passwordMessage}</Banner> : null}
          {passwordError ? <Banner tone="error">{passwordError}</Banner> : null}
          <div className="flex flex-wrap items-center gap-2">
            <Button type="submit" size="sm" disabled={passwordBusy || !current || !next || !confirm}>
              {passwordBusy ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
              保存新密码
            </Button>
            <span className="text-[0.66rem] text-faint">
              修改后当前会话保持有效；下次登录使用新密码。密码哈希由服务端保存。
            </span>
          </div>
        </form>
      </section>

      <section className="rounded-panel border border-line bg-panel p-3">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <h2 className="flex items-center gap-1.5 text-xs font-semibold text-soft">
            {totpEnabled ? <ShieldCheck className="size-3.5 text-ok" /> : <ShieldOff className="size-3.5" />}
            二次验证（TOTP）
          </h2>
          <span
            className={cn(
              "rounded border px-1.5 py-0.5 text-[0.62rem]",
              totpEnabled ? "border-ok/30 bg-ok/10 text-ok" : "border-line bg-sunken text-faint",
            )}
          >
            {totpEnabled ? "已启用" : "未启用"}
          </span>
        </div>

        {totpMessage ? <Banner tone="ok">{totpMessage}</Banner> : null}
        {totpError ? <Banner tone="error">{totpError}</Banner> : null}

        {!totpEnabled && !setup ? (
          <>
            <p className="mb-2 text-[0.7rem] leading-relaxed text-soft">
              开启后，每次登录都需要输入手机验证器上的 6 位验证码。启用过程中会显示一次性密钥，请立即保存到验证器。
            </p>
            <Button size="sm" onClick={() => void startSetup()} disabled={totpBusy}>
              {totpBusy ? <Loader2 className="size-3.5 animate-spin" /> : <ShieldCheck className="size-3.5" />}
              开始绑定
            </Button>
          </>
        ) : null}

        {setup ? (
          <div className="space-y-2">
            <Banner tone="info">
              在该浏览器打开验证器 App，选择「手动输入密钥」：粘贴下方密钥或 otpauth URI（二维码的文本降级形式），
              然后输入验证器显示的 6 位验证码完成启用。
            </Banner>
            <div className="space-y-1">
              <p className="text-[0.66rem] text-faint">密钥（一次性显示，请立即保存）</p>
              <div className="flex items-center gap-1.5">
                <code className="min-w-0 flex-1 break-all rounded border border-line bg-sunken px-2 py-1 font-mono text-[0.68rem]">
                  {setup.secret}
                </code>
                <Button size="icon-sm" variant="ghost" aria-label="复制密钥" onClick={() => void copy(setup.secret, "secret")}>
                  {copied === "secret" ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                </Button>
              </div>
            </div>
            <div className="space-y-1">
              <p className="text-[0.66rem] text-faint">otpauth URI</p>
              <div className="flex items-center gap-1.5">
                <code className="min-w-0 flex-1 truncate rounded border border-line bg-sunken px-2 py-1 font-mono text-[0.66rem]">
                  {setup.uri}
                </code>
                <Button size="icon-sm" variant="ghost" aria-label="复制 otpauth URI" onClick={() => void copy(setup.uri, "uri")}>
                  {copied === "uri" ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                </Button>
              </div>
            </div>
            <div className="flex flex-wrap items-end gap-2">
              <div className="space-y-1">
                <Label htmlFor="setup-code" className="text-[0.7rem] text-faint">
                  验证器上的 6 位验证码
                </Label>
                <Input
                  id="setup-code"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={6}
                  value={setupCode}
                  onChange={(event) => setSetupCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
                  className="h-9 w-40 text-center font-mono tracking-[0.3em]"
                />
              </div>
              <Button size="sm" onClick={() => void enableTotp()} disabled={totpBusy || setupCode.length !== 6}>
                {totpBusy ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
                启用二次验证
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setSetup(null);
                  setSetupCode("");
                }}
              >
                取消
              </Button>
            </div>
          </div>
        ) : null}

        {totpEnabled ? (
          <div className="mt-2 space-y-2 border-t border-line pt-2">
            <p className="text-[0.7rem] leading-relaxed text-soft">
              关闭二次验证需要当前验证器上的有效验证码（服务端拒绝无验证码的关闭请求）。关闭后仅密码即可登录。
            </p>
            <div className="flex flex-wrap items-end gap-2">
              <div className="space-y-1">
                <Label htmlFor="disable-code" className="text-[0.7rem] text-faint">
                  当前 6 位验证码
                </Label>
                <Input
                  id="disable-code"
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  maxLength={6}
                  value={disableCode}
                  onChange={(event) => setDisableCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
                  className="h-9 w-40 text-center font-mono tracking-[0.3em]"
                />
              </div>
              <Button
                size="sm"
                variant="destructive"
                onClick={() => void disableTotp()}
                disabled={totpBusy || disableCode.length !== 6}
              >
                关闭二次验证
              </Button>
            </div>
          </div>
        ) : null}
      </section>

      <section className="rounded-panel border border-line bg-panel p-3">
        <h2 className="mb-2 text-xs font-semibold text-soft">会话</h2>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[0.72rem]">
          <dt className="text-faint">控制台版本</dt>
          <dd className="font-mono text-soft">v{version || "—"}</dd>
          <dt className="text-faint">控制台域名</dt>
          <dd className="break-all text-soft">{consoleHost || "（服务端未配置）"}</dd>
          <dt className="text-faint">备用域名</dt>
          <dd className="break-all text-soft">{baseUrls.join("  |  ") || "—"}</dd>
          <dt className="text-faint">会话</dt>
          <dd className="text-soft">
            当前浏览器使用 HttpOnly 会话 Cookie；退出登录会清除本地会话与缓存。
          </dd>
        </dl>
        <div className="mt-2 flex flex-wrap gap-2">
          <Button size="sm" variant="outline" onClick={() => void logout()}>
            退出当前会话
          </Button>
        </div>
      </section>

      <p className="text-[0.66rem] leading-relaxed text-faint">
        二次验证密钥只在绑定过程中出现在本页；启用后服务端只保存密钥、不再回传。丢失验证器时需要通过服务端运维手段重置
        （当前版本不提供恢复码）。
      </p>
    </div>
  );
}