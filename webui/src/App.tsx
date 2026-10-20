import { useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "@/components/app-shell";
import { LiveBridge } from "@/components/live-bridge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { AuditPage } from "@/pages/audit";
import { DevicePage } from "@/pages/device";
import { LoginPage } from "@/pages/login";
import { SettingsPage } from "@/pages/settings";
import { UpdatesPage } from "@/pages/updates";
import { WallPage } from "@/pages/wall";
import { useAuth } from "@/store/auth";

export function App() {
  const { status, error, version, bootstrap, refresh } = useAuth();

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  if (status === "boot") {
    return (
      <div className="flex h-[100dvh] flex-col items-center justify-center gap-3">
        <Spinner className="size-5 text-brand" />
        <p className="text-xs text-soft">正在连接 Observatory…</p>
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className="flex h-[100dvh] items-center justify-center px-4">
        <div className="glass w-full max-w-sm rounded-panel border border-line p-6 text-center shadow-card">
          <p className="text-sm font-medium text-ink">{error}</p>
          <p className="mt-1.5 text-xs leading-relaxed text-soft">
            服务器的实时连接可能已中断。你的登录状态仍然保留，网络恢复后可以继续使用。
          </p>
          <Button className="mt-4 w-full" onClick={() => void refresh()}>
            重试
          </Button>
        </div>
      </div>
    );
  }

  if (status === "anonymous") {
    return <LoginPage version={version} />;
  }

  return (
    <>
      <LiveBridge />
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<WallPage />} />
          <Route path="devices/:uid" element={<DevicePage />} />
          <Route path="updates" element={<UpdatesPage />} />
          <Route path="audit" element={<AuditPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </>
  );
}