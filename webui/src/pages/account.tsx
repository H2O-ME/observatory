import { useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Check, Copy, KeyRound, LogOut, MonitorSmartphone, ShieldCheck, ShieldOff, UserRound } from "lucide-react";

import { GlowCard, GridPattern, ShimmerButton, Sparkles } from "@/components/aceternity";
import { Banner } from "@/components/async-state";
import { MetaChip, PageHeader } from "@/components/page-header";
import { Section, SectionHeader } from "@/components/section";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useViewport } from "@/hooks/use-viewport";
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
 *
 * Presentation (0.4): security cards in a two-column desktop grid, one column on
 * phones/watches; the TOTP setup panel is the only "lit" surface on the page.
 */
export function AccountPage() {
  const { totpEnabled, version, consoleHost, baseUrls, refresh, logout } = useAuth();
  const viewport = useViewport();
  const desktop = viewport === "desktop";
  const watch = viewport === "watch";
  const touch = !desktop;
  const stack = watch ? "space-y-3" : "space-y-5";

  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [passwordMessage, setPasswordMessage] = useState("");
  const [passwordError, setPasswordError] = useState("");

  const [setup, setSetup] = useState<{ secret: string; uri: string } | null>(null);
  const [setupCode, setSetupCode] = useState("");
  const [setupPassword, setSetupPassword] = useState("");
  const [disableCode, setDisableCode] = useState("");
  const [disablePassword, setDisablePassword] = useState("");
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

  const submitPassword = async (event: FormEvent) => {
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
    if (!setupPassword) {
      setTotpError("请输入当前控制台密码");
      return;
    }
    setTotpBusy(true);
    setTotpError("");
    setTotpMessage("");
    try {
      const result = await api.totpEnable(setup.secret, setupCode, setupPassword);
      setTotpMessage(result.message || "已开启二次验证");
      setSetup(null);
      setSetupCode("");
      setSetupPassword("");
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
    if (!disablePassword) {
      setTotpError("请输入当前控制台密码");
      return;
    }
    setTotpBusy(true);
    setTotpError("");
    setTotpMessage("");
    try {
      const result = await api.totpDisable(disableCode, disablePassword);
      setTotpMessage(result.message || "已关闭二次验证");
      setDisableCode("");
      setDisablePassword("");
      await refresh();
    } catch (failure) {
      setTotpError(isApiError(failure) ? failure.message : "关闭失败");
    } finally {
      setTotpBusy(false);
    }
  };

  return (
    <div className="w-full min-w-0">
      <PageHeader
        icon={UserRound}
        title="账户与安全"
        description="修改控制台密码、管理二次验证；新密码至少 10 位，开启/关闭 TOTP 都需要有效验证码与当前密码确认。"
      />

      <div className={cn("mt-4 min-w-0 sm:mt-5", stack)}>
        <div className="grid min-w-0 gap-5 lg:grid-cols-2">
          {/* ------------------------------------------------------ password card */}
          <Section title="修改密码" icon={KeyRound}>
            <form className="grid min-w-0 gap-3" onSubmit={submitPassword}>
              <PasswordField
                id="current-password"
                label="当前密码"
                autoComplete="current-password"
                value={current}
                onChange={setCurrent}
                touch={touch}
              />
              <PasswordField
                id="new-password"
                label="新密码"
                hint="至少 10 位"
                autoComplete="new-password"
                value={next}
                onChange={setNext}
                touch={touch}
              />
              <PasswordField
                id="confirm-password"
                label="确认新密码"
                autoComplete="new-password"
                value={confirm}
                onChange={setConfirm}
                touch={touch}
              />

              {passwordMessage ? <Banner tone="ok">{passwordMessage}</Banner> : null}
              {passwordError ? <Banner tone="error">{passwordError}</Banner> : null}

              <div className="grid min-w-0 border-t border-line pt-3">
                <ShimmerButton
                  type="submit"
                  busy={passwordBusy}
                  disabled={passwordBusy || !current || !next || !confirm}
                  className="justify-self-start sm:justify-self-end"
                >
                  <Check className="mr-1.5 size-4" />
                  保存新密码
                </ShimmerButton>
              </div>
            </form>
          </Section>

          {/* --------------------------------------------------------- TOTP card */}
          <GlowCard isStatic={!desktop} className="min-w-0" contentClassName="p-3 sm:p-4">
            <SectionHeader
              title="二次验证（TOTP）"
              icon={totpEnabled ? ShieldCheck : ShieldOff}
              actions={
                <MetaChip tone={totpEnabled ? "ok" : "default"}>{totpEnabled ? "已启用" : "未启用"}</MetaChip>
              }
            />

            <div className="mt-3 grid min-w-0 gap-3">
              {totpMessage ? <Banner tone="ok">{totpMessage}</Banner> : null}
              {totpError ? <Banner tone="error">{totpError}</Banner> : null}

              {!totpEnabled && !setup ? (
                <>
                  <p className="text-[0.8rem] leading-relaxed text-soft">
                    开启后每次登录需要验证器上的 6 位验证码；启用过程中会显示一次性密钥。
                  </p>
                  <div className="flex min-w-0">
                    <ShimmerButton busy={totpBusy} onClick={() => void startSetup()}>
                      <ShieldCheck className="mr-1.5 size-4" />
                      开始绑定
                    </ShimmerButton>
                  </div>
                </>
              ) : null}

              {setup ? (
                <div className="relative min-w-0 overflow-hidden rounded-xl border border-line bg-sunken p-3">
                  <GridPattern
                    className="pointer-events-none absolute inset-full opacity-30"
                    stroke="var(--c-grid-line)"
                    fade="radial"
                  />
                  <Sparkles className="opacity-70" count={10} />
                  <div className="relative grid min-w-0 gap-2.5">
                    <Banner tone="info">
                      在该浏览器打开验证器 App，选择「手动输入密钥」：粘贴下方密钥或 otpauth URI（二维码的文本降级形式），
                      然后输入验证器显示的 6 位验证码完成启用。
                    </Banner>

                    <div className="grid min-w-0 gap-1">
                      <p className="text-[0.7rem] text-faint">密钥（一次性显示，请立即保存）</p>
                      <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-1.5">
                        <code className="min-w-0 break-all rounded-lg border border-line bg-panel px-2.5 py-2 font-mono text-[0.72rem] leading-relaxed text-ink">
                          {setup.secret}
                        </code>
                        <Button
                          size="icon-sm"
                          variant="outline"
                          className={cn("shrink-0", touch && "size-11")}
                          aria-label="复制密钥"
                          title="复制密钥"
                          onClick={() => void copy(setup.secret, "secret")}
                        >
                          {copied === "secret" ? <Check className="size-4" /> : <Copy className="size-4" />}
                        </Button>
                      </div>
                    </div>

                    <div className="grid min-w-0 gap-1">
                      <p className="text-[0.7rem] text-faint">otpauth URI</p>
                      <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-1.5">
                        <code className="min-w-0 break-all rounded-lg border border-line bg-panel px-2.5 py-2 font-mono text-[0.72rem] leading-relaxed text-ink">
                          {setup.uri}
                        </code>
                        <Button
                          size="icon-sm"
                          variant="outline"
                          className={cn("shrink-0", touch && "size-11")}
                          aria-label="复制 otpauth URI"
                          title="复制 otpauth URI"
                          onClick={() => void copy(setup.uri, "uri")}
                        >
                          {copied === "uri" ? <Check className="size-4" /> : <Copy className="size-4" />}
                        </Button>
                      </div>
                    </div>

                    <div className="grid min-w-0 gap-2 sm:grid-cols-[minmax(0,9rem)_minmax(0,11rem)_auto_auto] sm:items-end">
                      <div className="min-w-0">
                        <Label htmlFor="setup-code" className="text-[0.72rem] text-faint">
                          验证器上的 6 位验证码
                        </Label>
                        <Input
                          id="setup-code"
                          inputMode="numeric"
                          autoComplete="one-time-code"
                          maxLength={6}
                          value={setupCode}
                          onChange={(event) => setSetupCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
                          className={cn("mt-1 h-9 text-center font-mono tracking-[0.3em]", touch && "tap")}
                        />
                      </div>
                      <div className="min-w-0">
                        <Label htmlFor="setup-password" className="text-[0.72rem] text-faint">
                          当前密码（防会话劫持）
                        </Label>
                        <Input
                          id="setup-password"
                          type="password"
                          autoComplete="current-password"
                          value={setupPassword}
                          onChange={(event) => setSetupPassword(event.target.value)}
                          className={cn("mt-1 h-9", touch && "tap")}
                        />
                      </div>
                      <ShimmerButton
                        busy={totpBusy}
                        disabled={totpBusy || setupCode.length !== 6 || !setupPassword}
                        onClick={() => void enableTotp()}
                      >
                        <Check className="mr-1.5 size-4" />
                        启用二次验证
                      </ShimmerButton>
                      <Button
                        size="sm"
                        variant="ghost"
                        className="h-11 sm:h-9"
                        onClick={() => {
                          setSetup(null);
                          setSetupCode("");
                        }}
                      >
                        取消
                      </Button>
                    </div>
                  </div>
                </div>
              ) : null}

              {totpEnabled ? (
                <div className="grid min-w-0 gap-2 border-t border-line pt-3">
                  <div className="grid min-w-0 gap-2 sm:grid-cols-[minmax(0,9rem)_minmax(0,11rem)_auto] sm:items-end">
                    <div className="min-w-0">
                      <Label htmlFor="disable-code" className="text-[0.72rem] text-faint">
                        当前 6 位验证码
                      </Label>
                      <Input
                        id="disable-code"
                        inputMode="numeric"
                        autoComplete="one-time-code"
                        maxLength={6}
                        value={disableCode}
                        onChange={(event) => setDisableCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
                        className={cn("mt-1 h-9 text-center font-mono tracking-[0.3em]", touch && "tap")}
                      />
                    </div>
                    <div className="min-w-0">
                      <Label htmlFor="disable-password" className="text-[0.72rem] text-faint">
                        当前密码（防会话劫持）
                      </Label>
                      <Input
                        id="disable-password"
                        type="password"
                        autoComplete="current-password"
                        value={disablePassword}
                        onChange={(event) => setDisablePassword(event.target.value)}
                        className={cn("mt-1 h-9", touch && "tap")}
                      />
                    </div>
                    <Button
                      size="sm"
                      variant="destructive"
                      className="h-11 sm:h-9"
                      onClick={() => void disableTotp()}
                      disabled={totpBusy || disableCode.length !== 6 || !disablePassword}
                    >
                      关闭二次验证
                    </Button>
                  </div>
                </div>
              ) : null}
            </div>
          </GlowCard>
        </div>

        {/* --------------------------------------------------------- session card */}
        <Section title="会话" icon={MonitorSmartphone}>
          <dl className="grid min-w-0 gap-0">
            <InfoRow label="控制台版本">
              <span className="tnum">v{version || "—"}</span>
            </InfoRow>
            <InfoRow label="控制台域名">
              <span className="break-all">{consoleHost || "（服务端未配置）"}</span>
            </InfoRow>
            <InfoRow label="备用域名">
              <span className="break-all">{baseUrls.join("  |  ") || "—"}</span>
            </InfoRow>
          </dl>
          <div className="mt-3 grid min-w-0">
            <Button size="sm" variant="outline" className="tap h-11 justify-self-start sm:h-9" onClick={() => void logout()}>
              <LogOut className="size-3.5" />
              退出当前会话
            </Button>
          </div>
        </Section>
      </div>
    </div>
  );
}

/** Label + input row; stacks on phones/watches, label column on desktop. */
function PasswordField({
  id,
  label,
  hint,
  autoComplete,
  value,
  onChange,
  touch,
}: {
  id: string;
  label: string;
  hint?: string;
  autoComplete: string;
  value: string;
  onChange: (value: string) => void;
  touch: boolean;
}) {
  return (
    <div className="grid min-w-0 gap-1.5 sm:grid-cols-[minmax(0,9rem)_minmax(0,1fr)] sm:items-center sm:gap-3">
      <div className="min-w-0">
        <Label htmlFor={id} className="text-[0.82rem] font-medium text-ink">
          {label}
        </Label>
        {hint ? <p className="mt-0.5 text-[0.68rem] leading-relaxed text-faint">{hint}</p> : null}
      </div>
      <Input
        id={id}
        type="password"
        autoComplete={autoComplete}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className={cn("h-9", touch && "tap")}
        required
      />
    </div>
  );
}

/** One session key/value line. */
function InfoRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,6.5rem)_minmax(0,1fr)] items-baseline gap-2 border-b border-line py-1.5 last:border-0 last:pb-0">
      <dt className="min-w-0 break-all text-[0.7rem] text-faint">{label}</dt>
      <dd className="min-w-0 break-all text-[0.78rem] leading-relaxed text-soft">{children}</dd>
    </div>
  );
}
