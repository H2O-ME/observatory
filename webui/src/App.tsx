import { useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Loader2, RadioTower, RefreshCw } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { GridPattern } from "@/components/aceternity/grid-pattern";
import { LiveBridge } from "@/components/live-bridge";
import { Button } from "@/components/ui/button";
import { AccountPage } from "@/pages/account";
import { AuditPage } from "@/pages/audit";
import { BackupPage } from "@/pages/backup";
import { DevicePage } from "@/pages/device";
import { LoginPage } from "@/pages/login";
import { SettingsPage } from "@/pages/settings";
import { SystemPage } from "@/pages/system";
import { UpdatesPage } from "@/pages/updates";
import { WallPage } from "@/pages/wall";
import { useAuth } from "@/store/auth";

/**
 * Route table. 0.2.x bookmarks (`/` and `/devices/:uid`) keep resolving to the
 * same element so nobody's saved link breaks.
 */
export function App() {
  const { status, error, version, bootstrap, refresh } = useAuth();

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  if (status === "boot") {
    return (
      <div className="relative flex app-vh flex-col items-center justify-center overflow-hidden px-4">
        <GridPattern className="opacity-50" stroke="var(--c-grid-line)" strokeDasharray="4 6" />
        <div className="surface-strong relative z-10 flex flex-col items-center px-8 py-7">
          <span className="relative flex size-12 items-center justify-center rounded-2xl border border-line bg-brand-soft text-brand">
            <RadioTower className="size-5" />
            <span className="absolute -right-1 -top-1 size-2.5 rounded-full bg-ok animate-sm-pulse" />
          </span>
          <p className="mt-4 text-[0.95rem] font-semibold text-ink">正在连接 Observatory</p>
          <p className="mt-1 flex items-center gap-1.5 text-[0.78rem] text-soft">
            <Loader2 className="size-3.5 animate-spin text-brand" />
            读取服务端会话…
          </p>
        </div>
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className="relative flex app-vh items-center justify-center overflow-hidden px-4">
        <GridPattern className="opacity-50" stroke="var(--c-grid-line)" strokeDasharray="4 6" />
        <div className="surface-strong relative z-10 w-full max-w-sm p-6 text-center">
          <p className="text-[0.95rem] font-semibold text-ink">{error}</p>
          <p className="mt-2 text-[0.8rem] leading-relaxed text-soft">
            控制台无法读取服务端会话。网络恢复后可以继续使用，登录状态会保留。
          </p>
          <Button className="mt-5 w-full" onClick={() => void refresh()}>
            <RefreshCw className="size-4" />
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
          <Route index element={<Navigate to="/wall" replace />} />
          <Route path="wall" element={<WallPage />} />
          <Route path="device/:uid/*" element={<DevicePage />} />
          {/* 0.2.x bookmark compatibility: same page, no redirect. */}
          <Route path="devices/:uid/*" element={<DevicePage />} />
          <Route path="backup" element={<BackupPage />} />
          <Route path="updates" element={<UpdatesPage />} />
          <Route path="audit" element={<AuditPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="system" element={<SystemPage />} />
          <Route path="account" element={<AccountPage />} />
          <Route path="login" element={<Navigate to="/wall" replace />} />
          <Route path="*" element={<Navigate to="/wall" replace />} />
        </Route>
      </Routes>
    </>
  );
}
