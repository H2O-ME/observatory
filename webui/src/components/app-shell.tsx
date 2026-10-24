/**
 * Application shell: header, navigation, theme, connection indicator.
 *
 * The theme has one decision and one switch (`<html class="dark">`). The mode is
 * resolved before the first paint by `index.html`, handed over through
 * `window.__OBS_THEME_MODE__`, and applied here with the *same* rule, so React
 * never disagrees with the first frame (no white flash) and `system` really
 * follows the OS.
 */

import { useCallback, useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  Archive,
  LogOut,
  MonitorSmartphone,
  Moon,
  PackageCheck,
  ScrollText,
  ServerCog,
  Settings as SettingsIcon,
  Sun,
  SunMoon,
  UserRound,
  WifiOff,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { useViewport } from "@/hooks/use-viewport";
import { useAuth } from "@/store/auth";
import { useLive } from "@/store/live";
import { cn } from "@/lib/utils";

const NAV = [
  { to: "/wall", label: "设备", icon: MonitorSmartphone },
  { to: "/backup", label: "备份", icon: Archive },
  { to: "/updates", label: "更新", icon: PackageCheck },
  { to: "/audit", label: "审计", icon: ScrollText },
  { to: "/system", label: "系统", icon: ServerCog },
  { to: "/settings", label: "设置", icon: SettingsIcon },
];

const THEME_KEY = "observatory.theme";

type ThemeMode = "system" | "light" | "dark";

function readStoredMode(): ThemeMode {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    /* storage disabled: fall through to the system mode */
  }
  return "system";
}

function prefersDark(): boolean {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function resolveDark(mode: ThemeMode): boolean {
  return mode === "dark" || (mode === "system" && prefersDark());
}

/** The single place that writes the theme to the document. */
function applyTheme(mode: ThemeMode): void {
  const dark = resolveDark(mode);
  const root = document.documentElement;
  root.classList.toggle("dark", dark);
  root.style.colorScheme = dark ? "dark" : "light";
}

function useTheme() {
  const [mode, setMode] = useState<ThemeMode>(() => window.__OBS_THEME_MODE__ ?? readStoredMode());

  useEffect(() => {
    applyTheme(mode);
    try {
      localStorage.setItem(THEME_KEY, mode);
    } catch {
      /* storage disabled: the choice still applies for this page */
    }
    if (mode !== "system") return;
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyTheme("system");
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, [mode]);

  const cycle = useCallback(() => {
    setMode((current) => (current === "system" ? "light" : current === "light" ? "dark" : "system"));
  }, []);

  return { mode, cycle };
}

const THEME_LABEL: Record<ThemeMode, string> = {
  system: "主题：跟随系统（点击切换为浅色）",
  light: "主题：浅色（点击切换为深色）",
  dark: "主题：深色（点击切换为跟随系统）",
};

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
          : "控制台与服务器的实时连接已断开，页面改用轮询，设备状态仍来自服务端"
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

const THEME_ICON = { system: SunMoon, light: Sun, dark: Moon } as const;

export function AppShell() {
  const viewport = useViewport();
  const { mode, cycle } = useTheme();
  const { version: authVersion, logout } = useAuth();
  const liveVersion = useLive((state) => state.version);
  const version = liveVersion || authVersion;
  const location = useLocation();
  const compact = viewport !== "desktop";
  const ThemeIcon = THEME_ICON[mode];

  const isActive = (to: string) => location.pathname === to || location.pathname.startsWith(`${to}/`);

  return (
    <div className="flex app-vh w-full flex-col overflow-hidden">
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
          <NavLink
            to="/account"
            aria-label="账号与安全"
            title="账号与安全"
            className="flex size-8 items-center justify-center rounded-lg text-soft transition-colors hover:bg-sunken hover:text-ink"
          >
            <UserRound className="size-4" />
          </NavLink>
          <Button variant="ghost" size="icon-sm" onClick={cycle} aria-label={THEME_LABEL[mode]} title={THEME_LABEL[mode]}>
            <ThemeIcon className="size-4" />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => void logout()}
            aria-label="退出登录"
            title="退出登录"
          >
            <LogOut className="size-4" />
          </Button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        {!compact ? (
          <aside className="flex w-52 shrink-0 flex-col gap-1 border-r border-line/70 px-2.5 py-3">
            {NAV.map((item) => {
              const Icon = item.icon;
              const active = isActive(item.to);
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
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
              <p>控制台 v{version || "—"}</p>
              <p>设备状态与时间均由服务端提供</p>
            </div>
          </aside>
        ) : null}

        <main className="min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-contain">
          <div
            className={cn(
              "mx-auto w-full",
              viewport === "watch"
                ? "max-w-none px-2 py-2 pb-24"
                : compact
                  ? "max-w-3xl px-3 py-3 pb-24"
                  : "max-w-[1400px] px-5 py-5",
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
            const active = isActive(item.to);
            return (
              <NavLink
                key={item.to}
                to={item.to}
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