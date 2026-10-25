/**
 * Application shell (0.3.3 全面重写)：三端一体的导航骨架。
 *
 * * **desktop** 左侧浮动玻璃栏（圆角大圆角 + 品牌标），当前项套
 *   Aceternity MovingBorder 流光描边；
 * * **mobile** 顶部紧凑工具栏 + 底部悬浮胶囊 Dock（不贴边，避开手势区）；
 * * **watch** 圆屏专用：**无底部 Dock**（会被圆角裁掉），导航是顶部第二行
 *   的横向滚动胶囊，主题/账号/退出收进胶囊行尾部。
 *
 * 主题与实时徽标的逻辑沿用 0.3.0：模式在首帧前由 index.html 解析，这里
 * 只做同一个开关，保证不闪白。
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

import { MovingBorder } from "@/components/aceternity/moving-border";
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

const THEME_ICON = { system: SunMoon, light: Sun, dark: Moon } as const;

function ConnectionDot({ verbose }: { verbose: boolean }) {
  const connected = useLive((state) => state.connected);
  const channels = useLive((state) => state.channels);
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 text-soft",
        verbose ? "text-xs" : "text-[0.7rem]",
      )}
      title={
        connected
          ? `控制台实时连接正常（WS 设备 ${channels.ws} / 轮询设备 ${channels.poll}）`
          : "控制台与服务器的实时连接已断开，页面改用轮询，设备状态仍来自服务端"
      }
    >
      {connected ? (
        <>
          <Activity className="size-4 text-ok" />
          {verbose && <span>实时</span>}
        </>
      ) : (
        <>
          <WifiOff className="size-4 text-warn" />
          {verbose && <span>轮询</span>}
        </>
      )}
    </span>
  );
}

function VersionChip({ version }: { version: string }) {
  return (
    <span className="inline-flex shrink-0 items-center rounded-md border border-line bg-sunken px-1.5 py-0.5 font-mono text-[0.7rem] font-medium text-soft">
      v{version || "—"}
    </span>
  );
}

/** 图标导航项三态渲染：desktop 侧栏 / mobile dock / watch 胶囊行。 */
function NavItem({
  item,
  active,
  variant,
}: {
  item: (typeof NAV)[number];
  active: boolean;
  variant: "rail" | "dock" | "pill";
}) {
  const Icon = item.icon;
  if (variant === "rail") {
    return (
      <NavLink to={item.to} className="block no-underline">
        {active ? (
          <MovingBorder duration={9000} containerClassName="rounded-xl">
            <span className="flex items-center gap-2.5 rounded-xl bg-panel px-3 py-2.5 text-sm font-medium text-brand shadow-card">
              <Icon className="size-[1.1rem]" />
              {item.label}
            </span>
          </MovingBorder>
        ) : (
          <span className="flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-sm text-soft transition-colors hover:bg-sunken hover:text-ink">
            <Icon className="size-[1.1rem]" />
            {item.label}
          </span>
        )}
      </NavLink>
    );
  }
  if (variant === "dock") {
    return (
      <NavLink
        to={item.to}
        className={cn(
          "flex min-h-[52px] flex-1 flex-col items-center justify-center gap-1 rounded-2xl no-underline transition-colors",
          active ? "bg-brand-soft text-brand" : "text-soft",
        )}
      >
        <Icon className="size-[1.2rem]" />
        <span className="text-[0.7rem] font-medium">{item.label}</span>
      </NavLink>
    );
  }
  // watch 胶囊：大触点、纯图标+短标签
  return (
    <NavLink
      to={item.to}
      className={cn(
        "flex h-11 shrink-0 items-center gap-1.5 rounded-full border px-3.5 no-underline transition-colors",
        active
          ? "border-transparent bg-brand text-brand-ink"
          : "border-line bg-panel text-soft",
      )}
    >
      <Icon className="size-4" />
      <span className="text-xs font-medium">{item.label}</span>
    </NavLink>
  );
}

export function AppShell() {
  const viewport = useViewport();
  const { mode, cycle } = useTheme();
  const { version: authVersion, logout } = useAuth();
  const liveVersion = useLive((state) => state.version);
  const version = liveVersion || authVersion;
  const location = useLocation();
  const ThemeIcon = THEME_ICON[mode];

  const isActive = (to: string) => location.pathname === to || location.pathname.startsWith(`${to}/`);
  const watch = viewport === "watch";
  const mobile = viewport === "mobile";

  return (
    <div className="flex app-vh w-full flex-col overflow-hidden">
      {/* ------------------------------------------------------------ 顶栏 */}
      <header
        className={cn(
          "glass-chrome z-30 flex shrink-0 items-center gap-2.5 border-b border-line",
          watch ? "h-11 px-2.5 pt-[env(safe-area-inset-top)]" : "h-14 px-4 pt-[env(safe-area-inset-top)] md:px-5",
        )}
      >
        <span
          className={cn(
            "flex items-center gap-2 font-semibold tracking-tight text-ink",
            watch ? "text-[0.8rem]" : "text-[0.95rem]",
          )}
        >
          <span className="flex size-6 items-center justify-center rounded-lg bg-brand text-[0.65rem] font-bold text-brand-ink">
            Ob
          </span>
          {!watch && "Observatory"}
        </span>
        {!watch && <VersionChip version={version} />}
        <div className="ml-auto flex items-center gap-1">
          {!watch && <ConnectionDot verbose={viewport === "desktop"} />}
          {watch && <ConnectionDot verbose={false} />}
          {!watch && (
            <>
              <NavLink
                to="/account"
                aria-label="账号与安全"
                title="账号与安全"
                className="flex size-9 items-center justify-center rounded-lg text-soft transition-colors hover:bg-sunken hover:text-ink"
              >
                <UserRound className="size-[1.1rem]" />
              </NavLink>
              <button
                type="button"
                onClick={cycle}
                aria-label={THEME_LABEL[mode]}
                title={THEME_LABEL[mode]}
                className="flex size-9 items-center justify-center rounded-lg text-soft transition-colors hover:bg-sunken hover:text-ink"
              >
                <ThemeIcon className="size-[1.1rem]" />
              </button>
              <button
                type="button"
                onClick={() => void logout()}
                aria-label="退出登录"
                title="退出登录"
                className="flex size-9 items-center justify-center rounded-lg text-soft transition-colors hover:bg-sunken hover:text-ink"
              >
                <LogOut className="size-[1.1rem]" />
              </button>
            </>
          )}
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* ------------------------------------------------ desktop 浮动侧栏 */}
        {viewport === "desktop" ? (
          <aside className="shrink-0 py-4 pl-3 pr-1.5">
            <div className="glass flex h-full w-[10.5rem] flex-col gap-1 rounded-window border border-line p-2.5 shadow-card">
              {NAV.map((item) => (
                <NavItem key={item.to} item={item} active={isActive(item.to)} variant="rail" />
              ))}
              <div className="mt-auto space-y-1 px-2 pb-1 text-[0.7rem] leading-relaxed text-faint">
                <p className="font-mono">v{version || "—"}</p>
                <p>设备状态与时间均由服务端提供</p>
              </div>
            </div>
          </aside>
        ) : null}

        <main className="min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-contain">
          <div
            className={cn(
              "mx-auto w-full",
              watch
                ? "max-w-none px-2.5 py-2.5"
                : mobile
                  ? "max-w-3xl px-3.5 py-4 pb-28"
                  : "max-w-[1500px] px-6 py-6",
            )}
          >
            <Outlet />
          </div>
        </main>
      </div>

      {/* ------------------------------------------------- watch 顶部胶囊行 */}
      {watch ? (
        <nav className="glass-chrome z-20 flex shrink-0 items-center gap-1.5 overflow-x-auto border-t border-line px-2.5 py-2 pb-[env(safe-area-inset-bottom)] scrollbar-none">
          {NAV.map((item) => (
            <NavItem key={item.to} item={item} active={isActive(item.to)} variant="pill" />
          ))}
          <span className="mx-0.5 h-6 w-px shrink-0 bg-line" />
          <NavLink
            to="/account"
            aria-label="账号与安全"
            className="flex h-11 shrink-0 items-center justify-center rounded-full border border-line bg-panel px-3 text-soft"
          >
            <UserRound className="size-4" />
          </NavLink>
          <button
            type="button"
            onClick={cycle}
            aria-label={THEME_LABEL[mode]}
            className="flex h-11 shrink-0 items-center justify-center rounded-full border border-line bg-panel px-3 text-soft"
          >
            <ThemeIcon className="size-4" />
          </button>
          <button
            type="button"
            onClick={() => void logout()}
            aria-label="退出登录"
            className="flex h-11 shrink-0 items-center justify-center rounded-full border border-line bg-panel px-3 text-soft"
          >
            <LogOut className="size-4" />
          </button>
        </nav>
      ) : null}

      {/* --------------------------------------------- mobile 悬浮胶囊 Dock */}
      {mobile ? (
        <nav className="fixed inset-x-0 bottom-0 z-30 flex justify-center px-4 pb-[max(env(safe-area-inset-bottom),0.75rem)]">
          <div className="glass flex w-full max-w-md items-stretch gap-0.5 rounded-window border border-line p-1.5 shadow-raised">
            {NAV.map((item) => (
              <NavItem key={item.to} item={item} active={isActive(item.to)} variant="dock" />
            ))}
          </div>
        </nav>
      ) : null}
    </div>
  );
}
