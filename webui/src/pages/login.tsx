import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { KeyRound, Loader2, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { isApiError } from "@/lib/api";
import { useAuth } from "@/store/auth";

interface Props {
  version: string;
}

/**
 * Login.
 *
 * The TOTP field is shown *before* the first attempt when the server's session
 * payload says `totp_enabled`, instead of guessing from an error string. The
 * error-string fallback stays for a server that enables TOTP between the session
 * probe and the login attempt.
 */
export function LoginPage({ version }: Props) {
  const login = useAuth((state) => state.login);
  const totpEnabled = useAuth((state) => state.totpEnabled);
  const consoleHost = useAuth((state) => state.consoleHost);
  const navigate = useNavigate();

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
      navigate("/wall", { replace: true });
    } catch (failure) {
      const message = isApiError(failure) ? failure.message : "登录失败";
      setError(message);
      if (/二次验证|验证码/.test(message)) setNeedTotp(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-[100dvh] items-center justify-center px-4 py-8">
      <div className="w-full max-w-sm">
        <div className="mb-5 text-center">
          <h1 className="text-xl font-semibold tracking-tight text-ink">Observatory</h1>
          <p className="mt-1 text-xs text-soft">受控端控制台</p>
          <p className="mt-2 inline-flex items-center rounded-md border border-line bg-sunken px-2 py-0.5 font-mono text-[0.7rem] text-soft">
            版本 v{version || "—"}
          </p>
        </div>

        <form onSubmit={submit} className="glass space-y-3.5 rounded-panel border border-line p-5 shadow-card">
          <div className="space-y-1.5">
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
            <div className="space-y-1.5">
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
              className="rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger"
            >
              {error}
            </p>
          ) : null}

          <Button type="submit" className="h-11 w-full text-[0.95rem]" disabled={busy || !password}>
            {busy ? <Loader2 className="size-4 animate-spin" /> : <KeyRound className="size-4" />}
            登录
          </Button>
        </form>

        <p className="mt-4 text-center text-[0.68rem] leading-relaxed text-faint">
          控制台域名为 {consoleHost || "（服务端未配置）"}
          <br />
          受控端可通过任一备用域名接入。
        </p>
      </div>
    </div>
  );
}