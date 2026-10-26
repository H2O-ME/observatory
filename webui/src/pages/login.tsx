import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  ArrowRight,
  Eye,
  EyeOff,
  KeyRound,
  LockKeyhole,
  Moon,
  RadioTower,
  ShieldCheck,
  Sun,
  SunMoon,
} from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { useViewport } from "@/hooks/use-viewport";
import { THEME_LABEL, useTheme, type ThemeMode } from "@/lib/theme";
import { isApiError } from "@/lib/api";
import { useAuth } from "@/store/auth";

interface Props {
  version: string;
}

const THEME_ICON: Record<ThemeMode, typeof Sun> = { system: SunMoon, light: Sun, dark: Moon };

const CAPABILITIES = [
  { icon: RadioTower, title: "单链路实时观测", text: "WebSocket 与长轮询同一套事实，断线自动降级" },
  { icon: ShieldCheck, title: "显式授权控制", text: "每次控制都有 TTL、撤销与全程审计" },
  { icon: LockKeyhole, title: "服务端时钟", text: "画面、命令与状态时间只来自服务端" },
] as const;

/**
 * Login.
 *
 * Same logic as before: TOTP appears when the server session says it is enabled
 * (or after the server asks for it), deep-link return is preserved, and the
 * console never invents error copy.
 */
export function LoginPage({ version }: Props) {
  const login = useAuth((state) => state.login);
  const totpEnabled = useAuth((state) => state.totpEnabled);
  const consoleHost = useAuth((state) => state.consoleHost);
  const navigate = useNavigate();
  const location = useLocation();
  const viewport = useViewport();
  const watch = viewport === "watch";
  const { mode, cycle } = useTheme();
  const ThemeIcon = THEME_ICON[mode];

  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [totp, setTotp] = useState("");
  const [needTotp, setNeedTotp] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const showTotp = totpEnabled || needTotp;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await login(password, showTotp ? totp : undefined);
      const target =
        location.pathname !== "/login" ? `${location.pathname}${location.search}` : "/wall";
      navigate(target, { replace: true });
    } catch (failure) {
      const message = isApiError(failure) ? failure.message : "登录失败";
      setError(message);
      if (/二次验证|验证码/.test(message)) setNeedTotp(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex app-vh-min w-full flex-col bg-background">
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-border px-4 sm:px-6">
        <span className="flex items-center gap-2.5">
          <span className="flex size-7 items-center justify-center rounded-md border border-border bg-card text-[0.6rem] font-semibold tracking-tight text-foreground">
            Ob
          </span>
          <span className="text-sm font-semibold tracking-tight text-foreground">
            Observatory
            {watch ? null : (
              <span className="ml-2 text-xs font-normal text-faint">受控端控制台</span>
            )}
          </span>
        </span>
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          onClick={cycle}
          aria-label={THEME_LABEL[mode]}
          title={THEME_LABEL[mode]}
        >
          <ThemeIcon className="size-4" />
        </Button>
      </header>

      <main className="flex flex-1 items-center justify-center px-4 py-8 sm:px-6">
        <div
          className={
            watch
              ? "w-full max-w-[19rem]"
              : "grid w-full max-w-5xl grid-cols-1 items-center gap-10 lg:grid-cols-[minmax(0,1fr)_22rem] lg:gap-16"
          }
        >
          {/* ------------------------------------------------------------ intro */}
          {watch ? null : (
            <section className="min-w-0">
              <p className="eyebrow mb-3">deep-field mission control</p>
              <h1 className="text-[2.1rem] leading-[1.05] font-semibold tracking-[-0.03em] text-foreground sm:text-[2.5rem]">
                Observatory
              </h1>
              <p className="mt-4 max-w-lg text-sm leading-relaxed text-muted-foreground">
                授权设备的观测、远程控制与数据归档，都在同一个控制台里。
              </p>
              <ul className="mt-8 max-w-md border-t border-border">
                {CAPABILITIES.map((item) => {
                  const Icon = item.icon;
                  return (
                    <li
                      key={item.title}
                      className="flex items-start gap-3 border-b border-border py-3"
                    >
                      <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
                      <span className="min-w-0">
                        <span className="block text-sm font-medium text-foreground">
                          {item.title}
                        </span>
                        <span className="mt-0.5 block text-xs leading-relaxed text-muted-foreground">
                          {item.text}
                        </span>
                      </span>
                    </li>
                  );
                })}
              </ul>
            </section>
          )}

          {/* ------------------------------------------------------------- form */}
          <div className={watch ? "rounded-2xl bg-card p-4 shadow-sm" : "rounded-2xl bg-card p-6 shadow-sm"}>
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                {watch ? (
                  <h2 className="text-sm font-medium">登录</h2>
                ) : (
                  <>
                    <p className="eyebrow">authorized access</p>
                    <h2 className="mt-2 text-lg font-semibold tracking-[-0.02em]">登录控制台</h2>
                    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                      需要控制台密码{showTotp ? "与验证器动态码" : ""}。会话仅保存在当前浏览器。
                    </p>
                  </>
                )}
              </div>
              <Badge variant="neutral" className="tnum mt-0.5 shrink-0">
                v{version || "—"}
              </Badge>
            </div>

            <form onSubmit={submit} className={watch ? "mt-3" : "mt-5"}>
              <FieldGroup className="gap-3">
                <Field>
                  <FieldLabel htmlFor="password" className="text-xs">
                    <KeyRound className="size-3.5 text-faint" />
                    控制台密码
                  </FieldLabel>
                  <div className="relative min-w-0">
                    <Input
                      id="password"
                      type={showPassword ? "text" : "password"}
                      autoComplete="current-password"
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                      placeholder="请输入密码"
                      className="pr-10"
                      required
                      autoFocus={!watch}
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword((current) => !current)}
                      aria-label={showPassword ? "隐藏密码" : "显示密码"}
                      title={showPassword ? "隐藏密码" : "显示密码"}
                      className="absolute top-0 right-0 flex h-9 w-9 items-center justify-center text-faint hover:text-foreground"
                    >
                      {showPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                    </button>
                  </div>
                </Field>

                {showTotp ? (
                  <Field>
                    <FieldLabel htmlFor="totp" className="text-xs">
                      <ShieldCheck className="size-3.5 text-faint" />
                      二次验证码
                    </FieldLabel>
                    <Input
                      id="totp"
                      inputMode="numeric"
                      autoComplete="one-time-code"
                      value={totp}
                      onChange={(event) => setTotp(event.target.value.replace(/\D/g, "").slice(0, 6))}
                      placeholder="6 位数字"
                      className="h-10 text-center font-mono text-lg tracking-[0.3em]"
                      maxLength={6}
                      autoFocus={totpEnabled && !watch}
                    />
                  </Field>
                ) : null}

                {error ? (
                  <Alert variant="destructive" role="alert" aria-live="polite">
                    <AlertDescription className="break-words text-destructive">{error}</AlertDescription>
                  </Alert>
                ) : null}

                <Button type="submit" disabled={busy || !password} className="w-full">
                  {busy ? <Spinner className="size-4" /> : <KeyRound className="size-4" />}
                  {busy ? "正在验证…" : "进入控制台"}
                  {busy ? null : <ArrowRight className="size-4" />}
                </Button>

                {watch ? null : (
                  <p className="text-xs leading-relaxed text-faint">
                    继续即表示你已获得目标设备的书面授权。连续失败会触发服务端限流。
                  </p>
                )}
              </FieldGroup>
            </form>

            <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3 text-xs text-faint">
              <span className="min-w-0 truncate">{consoleHost || "控制台域名未配置"}</span>
              <span className="tnum shrink-0">v{version || "—"}</span>
            </div>
          </div>
        </div>
      </main>

      <footer className="shrink-0 px-4 pb-5 text-center text-xs leading-relaxed text-faint">
        控制台域名 {consoleHost || "（服务端未配置）"} · 受控端可通过任一备用域名接入
      </footer>
    </div>
  );
}
