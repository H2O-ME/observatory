import { useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "@/components/app-shell";
import { LiveBridge } from "@/components/live-bridge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
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
 * Route table.
 *
 * The 0.3.0 URLs are `/wall`, `/device/:uid/{overview,live,timeline,files,commands}`,
 * `/backup`, `/audit`, `/settings`, `/updates` (spec §2.2). The 0.2.x bookmarks
 * `/` and `/devices/:uid` still resolve — same element, no redirect — so nobody's
 * saved link breaks.
 */
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
            控制台无法读取服务端会话。网络恢复后可以继续使用，登录状态会保留。
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