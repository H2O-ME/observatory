import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { KeyRound, Loader2, ShieldCheck } from "lucide-react";

import { GridPattern } from "@/components/aceternity/grid-pattern";
import { HoverBorderGradient } from "@/components/aceternity/hover-border-gradient";
import { Meteors } from "@/components/aceternity/meteors";
import { Spotlight } from "@/components/aceternity/spotlight";
import { TextGenerateEffect } from "@/components/aceternity/text-generate-effect";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useViewport } from "@/hooks/use-viewport";
import { isApiError } from "@/lib/api";
import { useAuth } from "@/store/auth";

interface Props {
  version: string;
}

/**
 * Login（0.3.3 Aceternity 重写）。
 *
 * 逻辑不变：TOTP 字段在服务端 session 声明 `totp_enabled` 时首渲染即出现，
 * 错误串回退保留；F40 深链接回跳保留。视觉层换成 Spotlight + 网格 + 流星
 * 的组合——手表端（无 hover、省电）自动去掉流星与网格动画。
 */
export function LoginPage({ version }: Props) {
  const login = useAuth((state) => state.login);
  const totpEnabled = useAuth((state) => state.totpEnabled);
  const consoleHost = useAuth((state) => state.consoleHost);
  const navigate = useNavigate();
  const location = useLocation();
  const viewport = useViewport();
  const watch = viewport === "watch";

  const [password, setPassword] = useState("");
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
      // F40: 会话过期被踢到登录页时 URL 未变（App 匿名态不走路由），登录后
      // 回跳原深链接；直登 /login 才去设备墙。
      const target = location.pathname !== "/login" ? `${location.pathname}${location.search}` : "/wall";
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
    <div className="relative flex app-vh-min items-center justify-center overflow-hidden px-4 py-8">
      {/* 背景层：网格 + 顶光 + 流星（手表端只留顶光） */}
      {!watch ? <GridPattern strokeDasharray="4 4" className="opacity-60 [mask-image:radial-gradient(ellipse_70%_60%_at_50%_40%,#000,transparent)]" /> : null}
      <Spotlight className="left-[-40%] top-[-10%] md:left-[-15%]" />
      {!watch ? <Meteors number={10} /> : null}

      <div className="relative z-10 w-full max-w-sm">
        <div className="mb-6 text-center">
          <h1 className="text-2xl font-semibold tracking-tight text-ink">
            <TextGenerateEffect words="Observatory" duration={0.6} />
          </h1>
          <p className="mt-1.5 text-sm text-soft">
            <TextGenerateEffect words="受控端控制台" duration={0.5} />
          </p>
          <p className="mt-2.5 inline-flex items-center rounded-md border border-line bg-sunken px-2 py-0.5 font-mono text-[0.72rem] text-soft">
            版本 v{version || "—"}
          </p>
        </div>

        <form onSubmit={submit} className="glass-raised relative space-y-4 rounded-window border border-line p-6 shadow-pop">
          <Spotlight className="left-[-30%] top-[-25%] h-[130%] w-[160%] opacity-25" />
          <div className="relative space-y-1.5">
            <Label htmlFor="password" className="text-xs text-soft">
              控制台密码
            </Label>
            <Input
              id="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="请输入密码"
              className="h-11 text-base"
              required
              autoFocus
            />
          </div>

          {showTotp ? (
            <div className="relative space-y-1.5">
              <Label htmlFor="totp" className="flex items-center gap-1.5 text-xs text-soft">
                <ShieldCheck className="size-3.5" />
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
                autoFocus={totpEnabled}
              />
            </div>
          ) : null}

          {error ? (
            <p
              role="alert"
              aria-live="polite"
              className="relative rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger"
            >
              {error}
            </p>
          ) : null}

          <HoverBorderGradient
            type="submit"
            duration={2}
            containerClassName="relative w-full"
            className="w-full text-[0.95rem]"
            disabled={busy || !password}
          >
            <span className="flex items-center justify-center gap-2">
              {busy ? <Loader2 className="size-4 animate-spin" /> : <KeyRound className="size-4" />}
              登录
            </span>
          </HoverBorderGradient>
        </form>

        <p className="mt-5 text-center text-[0.72rem] leading-relaxed text-faint">
          控制台域名为 {consoleHost || "（服务端未配置）"}
          <br />
          受控端可通过任一备用域名接入。
        </p>
      </div>
    </div>
  );
}
