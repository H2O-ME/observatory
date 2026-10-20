import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  LogOut,
  MonitorSmartphone,
  Moon,
  PackageCheck,
  ScrollText,
  Settings as SettingsIcon,
  Sun,
  WifiOff,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { useViewport } from "@/hooks/use-viewport";
import { useAuth } from "@/store/auth";
import { useLive } from "@/store/live";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/", label: "设备", icon: MonitorSmartphone, end: true },
  { to: "/updates", label: "更新", icon: PackageCheck, end: false },
  { to: "/audit", label: "审计", icon: ScrollText, end: false },
  { to: "/settings", label: "设置", icon: SettingsIcon, end: false },
];

function useTheme() {
  const [dark, setDark] = useState(() => {
    try {
      const stored = localStorage.getItem("observatory.theme");
      if (stored) return stored === "dark";
    } catch {
      /* ignore */
    }
    return true;
  });

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    try {
      localStorage.setItem("observatory.theme", dark ? "dark" : "light");
    } catch {
      /* ignore */
    }
  }, [dark]);

  return { dark, toggle: () => setDark((value) => !value) };
}

function VersionChip({ version }: { version: string }) {
  return (
    <span className="inline-flex shrink-0 items-center rounded-md border border-line bg-sunken px-1.5 py-0.5 font-mono text-[0.68rem] font-medium text-soft">
      v{version || "—"}
    </span>
  );
}

function ConnectionDot() {
  const connected = useLive((state) => state.connected);
  const channels = useLive((state) => state.channels);
  return (
    <span
      className="inline-flex shrink-0 items-center gap-1.5 text-[0.68rem] text-soft"
      title={
        connected
          ? `控制台实时连接正常（WS 设备 ${channels.ws} / 轮询设备 ${channels.poll}）`
          : "控制台与服务器的实时连接已断开，状态显示可能滞后"
      }
    >
      {connected ? (
        <>
          <Activity className="size-3.5 text-ok" />
          <span className="hidden sm:inline">实时</span>
        </>
      ) : (
        <>
          <WifiOff className="size-3.5 text-warn" />
          <span className="hidden sm:inline">断开</span>
        </>
      )}
    </span>
  );
}

export function AppShell() {
  const viewport = useViewport();
  const { dark, toggle } = useTheme();
  const { version: authVersion, logout } = useAuth();
  const liveVersion = useLive((state) => state.version);
  const version = liveVersion || authVersion;
  const location = useLocation();
  const compact = viewport !== "desktop";

  return (
    <div className="flex h-[100dvh] w-full flex-col overflow-hidden">
      <header
        className={cn(
          "glass-chrome z-20 flex shrink-0 items-center gap-2 border-b border-line px-3",
          compact ? "h-12 pt-[env(safe-area-inset-top)]" : "h-14 px-5",
        )}
      >
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-sm font-semibold tracking-tight text-ink">Observatory</span>
          <VersionChip version={version} />
        </div>
        <div className="ml-auto flex items-center gap-1.5">
          <ConnectionDot />
          <Button variant="ghost" size="icon-sm" onClick={toggle} aria-label="切换主题">
            {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </Button>
          <Button variant="ghost" size="icon-sm" onClick={() => void logout()} aria-label="退出登录">
            <LogOut className="size-4" />
          </Button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        {!compact ? (
          <aside className="flex w-52 shrink-0 flex-col gap-1 border-r border-line/70 px-2.5 py-3">
            {NAV.map((item) => {
              const Icon = item.icon;
              const active = item.end ? location.pathname === item.to : location.pathname.startsWith(item.to);
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={cn(
                    "flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm no-underline transition-colors",
                    active ? "bg-brand-soft font-medium text-brand" : "text-soft hover:bg-sunken hover:text-ink",
                  )}
                >
                  <Icon className="size-4" />
                  {item.label}
                </NavLink>
              );
            })}
            <div className="mt-auto px-1 text-[0.66rem] leading-relaxed text-faint">
              <p>受控端自动更新已启用</p>
              <p>版本 {version || "—"}</p>
            </div>
          </aside>
        ) : null}

        <main className="min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-contain">
          <div
            className={cn(
              "mx-auto w-full",
              viewport === "watch" ? "max-w-none px-2 py-2 pb-24" : compact ? "max-w-3xl px-3 py-3 pb-24" : "max-w-[1400px] px-5 py-5",
            )}
          >
            <Outlet />
          </div>
        </main>
      </div>

      {compact ? (
        <nav className="glass-chrome fixed inset-x-0 bottom-0 z-30 flex items-stretch border-t border-line pb-[env(safe-area-inset-bottom)]">
          {NAV.map((item) => {
            const Icon = item.icon;
            const active = item.end ? location.pathname === item.to : location.pathname.startsWith(item.to);
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                className={cn(
                  "flex min-h-[56px] flex-1 flex-col items-center justify-center gap-0.5 no-underline transition-colors",
                  active ? "text-brand" : "text-soft",
                )}
              >
                <Icon className={viewport === "watch" ? "size-5" : "size-[1.15rem]"} />
                <span className={cn("font-medium", viewport === "watch" ? "text-[0.6rem]" : "text-[0.65rem]")}>
                  {item.label}
                </span>
              </NavLink>
            );
          })}
        </nav>
      ) : null}
    </div>
  );
}