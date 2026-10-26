import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  ArrowRight,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  LockKeyhole,
  Moon,
  RadioTower,
  ShieldCheck,
  Sun,
  SunMoon,
} from "lucide-react";

import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useViewport } from "@/hooks/use-viewport";
import { THEME_LABEL, useTheme, type ThemeMode } from "@/lib/theme";
import { isApiError } from "@/lib/api";
import { useAuth } from "@/store/auth";

interface Props {
  version: string;
}

const THEME_ICON: Record<ThemeMode, typeof Sun> = { system: SunMoon, light: Sun, dark: Moon };

/**
 * Login (0.4 rewrite).
 *
 * Same logic as before: TOTP appears when the server session says it is enabled
 * (or after the server asks for it), deep-link return is preserved, and the
 * console never invents error copy. The visual is a flat mission-control hero
 * (beams + engineering grid), collapsing to a single compact card on the
 * watch.
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
        location.pathname !== "/login"
          ? `${location.pathname}${location.search}`
          : "/wall";
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
    <div className="relative flex app-vh-min w-full flex-col overflow-hidden">
      <div className="relative z-10 flex min-h-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between px-4 sm:px-6">
          <span className="flex items-center gap-2.5">
            <span className="relative flex size-8 items-center justify-center rounded-md border border-line bg-brand-soft text-brand">
              <span className="text-[0.62rem] font-bold">Ob</span>
            </span>
            <span className="text-[0.9rem] font-semibold tracking-tight text-ink">
              Observatory
              {!watch ? <span className="ml-2 text-[0.72rem] font-normal text-faint">受控端控制台</span> : null}
            </span>
          </span>
          <button
            type="button"
            onClick={cycle}
            aria-label={THEME_LABEL[mode]}
            title={THEME_LABEL[mode]}
            className="flex size-8 items-center justify-center rounded-md border border-line bg-sunken text-soft transition-colors hover:text-ink"
          >
            <ThemeIcon className="size-4" />
          </button>
        </header>

        <main className="flex flex-1 items-center justify-center px-4 pb-10 pt-2 sm:px-6">
          <div className={watch ? "w-full max-w-[19rem]" : "grid w-full max-w-5xl grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_26rem] lg:items-center lg:gap-10"}>
            {/* --------------------------------------------------------- hero */}
            <section className={watch ? "hidden" : "relative min-w-0"}>
              <div className="relative">
                <p className="eyebrow mb-3 flex items-center gap-2">
                  <span className="inline-block size-1.5 rounded-full bg-brand-3" />
                  deep-field mission control
                </p>
                <h1 className="text-[2.2rem] font-semibold tracking-[-0.03em] text-ink sm:text-[2.6rem]">
                  Observatory
                </h1>
                <p className="mt-4 max-w-lg text-[0.98rem] leading-relaxed text-soft">
                  授权设备的观测、远程控制与数据归档，都在同一个深空控制台里。
                </p>
                <ul className="mt-8 grid max-w-md grid-cols-1 gap-2.5">
                  {[
                    { icon: RadioTower, title: "单链路实时观测", text: "WebSocket 与长轮询同一套事实，断线自动降级" },
                    { icon: ShieldCheck, title: "显式授权控制", text: "每次控制都有 TTL、撤销与全程审计" },
                    { icon: LockKeyhole, title: "服务端时钟", text: "画面、命令与状态时间只来自服务端" },
                  ].map((item) => {
                    const Icon = item.icon;
                    return (
                      <li key={item.title} className="flex items-start gap-2.5 rounded-md border border-line bg-sunken px-3 py-2.5">
                        <span className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-md bg-panel text-soft">
                          <Icon className="size-3.5" />
                        </span>
                        <span className="min-w-0">
                          <span className="block text-[0.82rem] font-semibold text-ink">{item.title}</span>
                          <span className="block text-[0.72rem] leading-relaxed text-faint">{item.text}</span>
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </div>
            </section>

            {/* --------------------------------------------------------- form */}
            <form
              onSubmit={submit}
              className={
                watch
                  ? "surface-strong relative p-3"
                  : "surface-strong relative p-5 sm:p-6"
              }
            >
              {!watch ? (
                <div className="mb-5">
                  <p className="eyebrow mb-2">authorized access</p>
                  <h2 className="text-[1.25rem] font-semibold tracking-tight text-ink">登录控制台</h2>
                  <p className="mt-1 text-[0.8rem] leading-relaxed text-soft">
                    需要控制台密码{showTotp ? "与验证器动态码" : ""}。会话仅保存在当前浏览器。
                  </p>
                </div>
              ) : (
                <div className="mb-3 flex items-center justify-between">
                  <span className="text-[0.82rem] font-semibold text-ink">登录</span>
                  <span className="tnum rounded-md border border-line bg-sunken px-1.5 py-0.5 text-[0.62rem] text-faint">
                    v{version || "—"}
                  </span>
                </div>
              )}

              <div className="space-y-3">
                <div className="space-y-1.5">
                  <Label htmlFor="password" className="flex items-center gap-1.5">
                    <KeyRound className="size-3.5 text-faint" />
                    控制台密码
                  </Label>
                  <div className="relative min-w-0">
                    <Input
                      id="password"
                      type={showPassword ? "text" : "password"}
                      autoComplete="current-password"
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                      placeholder="请输入密码"
                      className="h-11 pr-11 text-base"
                      required
                      autoFocus={!watch}
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword((current) => !current)}
                      aria-label={showPassword ? "隐藏密码" : "显示密码"}
                      title={showPassword ? "隐藏密码" : "显示密码"}
                      className="absolute top-0 right-0 flex h-11 w-11 items-center justify-center text-faint"
                    >
                      {showPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                    </button>
                  </div>
                </div>

                {showTotp ? (
                  <div className="space-y-1.5">
                    <Label htmlFor="totp" className="flex items-center gap-1.5">
                      <ShieldCheck className="size-3.5 text-faint" />
                      二次验证码
                    </Label>
                    <Input
                      id="totp"
                      inputMode="numeric"
                      autoComplete="one-time-code"
                      value={totp}
                      onChange={(event) => setTotp(event.target.value.replace(/\D/g, "").slice(0, 6))}
                      placeholder="6 位数字"
                      className="h-11 text-center font-mono text-lg tracking-[0.3em]"
                      maxLength={6}
                      autoFocus={totpEnabled && !watch}
                    />
                  </div>
                ) : null}

                {error ? (
                  <p
                    role="alert"
                    aria-live="polite"
                    className="surface-danger break-words px-3 py-2 text-[0.8rem] leading-relaxed text-danger"
                  >
                    {error}
                  </p>
                ) : null}

                <button
                  type="submit"
                  disabled={busy || !password}
                  className="flex h-10 w-full items-center justify-center gap-2 rounded-md bg-brand text-[0.9rem] font-medium text-brand-ink transition-opacity disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {busy ? <Loader2 className="size-4 animate-spin" /> : <KeyRound className="size-4" />}
                  {busy ? "正在验证…" : "进入控制台"}
                  {!busy ? <ArrowRight className="size-4" /> : null}
                </button>

                {watch ? null : (
                  <p className="text-[0.7rem] leading-relaxed text-faint">
                    继续即表示你已获得目标设备的书面授权。连续失败会触发服务端限流。
                  </p>
                )}
              </div>

              <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3">
                <span className="inline-flex items-center gap-1.5 rounded-full border border-line bg-sunken px-2 py-0.5 text-[0.66rem] text-faint">
                  v{version || "—"}
                </span>
                <span className="min-w-0 truncate text-[0.66rem] text-faint">
                  {consoleHost || "控制台域名未配置"}
                </span>
              </div>
            </form>
          </div>
        </main>

        <footer className="shrink-0 px-4 pb-5 text-center text-[0.66rem] leading-relaxed text-faint">
          控制台域名 {consoleHost || "（服务端未配置）"} · 受控端可通过任一备用域名接入
        </footer>
      </div>
    </div>
  );
}
