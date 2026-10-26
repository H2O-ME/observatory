import { useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { RadioTower, RefreshCw } from "lucide-react";

import { AppShell } from "@/components/app-shell";
import { LiveBridge } from "@/components/live-bridge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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
      <div className="flex app-vh flex-col items-center justify-center bg-background px-4">
        <Card className="w-full max-w-xs">
          <CardHeader className="items-center text-center">
            <span className="mx-auto flex size-10 items-center justify-center rounded-md border border-border bg-muted text-primary">
              <RadioTower className="size-5" />
            </span>
            <CardTitle className="mt-2 text-base">正在连接 Observatory</CardTitle>
            <CardDescription className="flex items-center justify-center gap-1.5">
              <Spinner className="size-3.5 text-primary" />
              读取服务端会话…
            </CardDescription>
          </CardHeader>
        </Card>
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className="flex app-vh items-center justify-center bg-background px-4">
        <Card className="w-full max-w-sm">
          <CardHeader>
            <CardTitle className="text-base">{error}</CardTitle>
            <CardDescription>
              控制台无法读取服务端会话。网络恢复后可以继续使用，登录状态会保留。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button className="w-full" onClick={() => void refresh()}>
              <RefreshCw className="size-4" />
              重试
            </Button>
          </CardContent>
        </Card>
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
