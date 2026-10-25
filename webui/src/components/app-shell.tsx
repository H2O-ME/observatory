/**
 * Application shell.
 *
 * One component, three navigation variants:
 *  - desktop: a floating glass sidebar with the brand, 6 destinations and a
 *    live-status footer;
 *  - mobile: a compact top bar + Aceternity FloatingDock (5 visible entries;
 *    the fifth opens a "more" sheet with system/settings/account);
 *  - watch: a 40px brand bar + icon-only compact dock, no bottom overlap.
 *
 * The visual language lives in DESIGN_SYSTEM.md; this file only wires it to the
 * router, auth store and live socket state.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  Activity,
  Archive,
  ChevronRight,
  LogOut,
  MonitorSmartphone,
  Moon,
  MoreHorizontal,
  PackageCheck,
  ScrollText,
  ServerCog,
  Settings as SettingsIcon,
  Sun,
  SunMoon,
  UserRound,
  WifiOff,
} from "lucide-react";

import {
  AnimatedTooltip,
  FloatingDock,
  GridPattern,
  MovingBorder,
} from "@/components/aceternity";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useViewport } from "@/hooks/use-viewport";
import { THEME_LABEL, useTheme, type ThemeMode } from "@/lib/theme";
import { cn } from "@/lib/utils";
import { useAuth } from "@/store/auth";
import { useLive } from "@/store/live";

type Icon = typeof MonitorSmartphone;

interface NavEntry {
  to: string;
  label: string;
  hint: string;
  icon: Icon;
}

const NAV: NavEntry[] = [
  { to: "/wall", label: "设备墙", hint: "全部受控端", icon: MonitorSmartphone },
  { to: "/backup", label: "归档", hint: "远端备份", icon: Archive },
  { to: "/updates", label: "更新", hint: "受控端版本", icon: PackageCheck },
  { to: "/audit", label: "审计", hint: "操作留痕", icon: ScrollText },
  { to: "/system", label: "系统", hint: "服务端状态", icon: ServerCog },
  { to: "/settings", label: "设置", hint: "全局配置", icon: SettingsIcon },
];

/** Mobile dock shows the four most-used destinations + a more sheet. */
const DOCK_ROUTES = ["/wall", "/backup", "/updates", "/audit"];

const THEME_ICON: Record<ThemeMode, Icon> = {
  system: SunMoon,
  light: Sun,
  dark: Moon,
};

function useActivePath() {
  const { pathname } = useLocation();
  return useCallback(
    (to: string) => pathname === to || pathname.startsWith(`${to}/`),
    [pathname],
  );
}


function LiveDot({ verbose = false }: { verbose?: boolean }) {
  const connected = useLive((state) => state.connected);
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-[0.7rem] font-medium",
        connected
          ? "border-line bg-ok-soft text-ok"
          : "border-line bg-warn-soft text-warn",
      )}
      title={
        connected
          ? "控制台实时连接正常"
          : "控制台与服务器的实时连接已断开，页面改用轮询"
      }
    >
      {connected ? (
        <Activity className="size-3.5 animate-sm-pulse" />
      ) : (
        <WifiOff className="size-3.5" />
      )}
      {verbose ? <span>{connected ? "实时" : "轮询"}</span> : null}
    </span>
  );
}

function BrandMark({ compact = false }: { compact?: boolean }) {
  return (
    <span className="flex min-w-0 items-center gap-2.5">
      <span className="ace-brand-orb relative flex size-9 shrink-0 items-center justify-center rounded-2xl border border-line bg-brand-soft text-brand">
        <span className="ace-brand-orb__ring" aria-hidden="true" />
        <span className="text-[0.66rem] font-bold tracking-tight">Ob</span>
      </span>
      {!compact ? (
        <span className="min-w-0">
          <span className="block truncate text-[0.95rem] font-semibold tracking-tight text-ink">
            Observatory
          </span>
          <span className="eyebrow block text-[0.55rem] leading-tight">mission control</span>
        </span>
      ) : null}
    </span>
  );
}

function ThemeButton({ className }: { className?: string }) {
  const { mode, cycle } = useTheme();
  const Icon = THEME_ICON[mode];
  return (
    <button
      type="button"
      onClick={cycle}
      aria-label={THEME_LABEL[mode]}
      title={THEME_LABEL[mode]}
      className={cn(
        "flex size-9 shrink-0 items-center justify-center rounded-xl border border-line bg-sunken text-soft transition-colors hover:text-ink",
        className,
      )}
    >
      <Icon className="size-4" />
    </button>
  );
}

function AccountLink({ className }: { className?: string }) {
  return (
    <NavLink
      to="/account"
      aria-label="账户与安全"
      title="账户与安全"
      className={cn(
        "flex size-9 shrink-0 items-center justify-center rounded-xl border border-line bg-sunken text-soft no-underline transition-colors hover:text-ink",
        className,
      )}
    >
      <UserRound className="size-4" />
    </NavLink>
  );
}

function LogoutButton({ className }: { className?: string }) {
  const logout = useAuth((state) => state.logout);
  return (
    <button
      type="button"
      onClick={() => void logout()}
      aria-label="退出登录"
      title="退出登录"
      className={cn(
        "flex size-9 shrink-0 items-center justify-center rounded-xl border border-line bg-sunken text-soft transition-colors hover:text-danger",
        className,
      )}
    >
      <LogOut className="size-4" />
    </button>
  );
}

function DesktopSidebar({ active }: { active: (to: string) => boolean }) {
  const version = useAuth((state) => state.version);
  const liveVersion = useLive((state) => state.version);
  const connected = useLive((state) => state.connected);

  return (
    <aside className="hidden w-[252px] shrink-0 py-5 pl-5 pr-0 lg:block">
      <div className="surface-strong flex h-full w-full flex-col overflow-hidden p-3">
        <div className="px-2 pb-6 pt-2">
          <BrandMark />
          <p className="mt-3 flex items-center gap-2 text-[0.7rem] text-faint">
            <span className={cn("size-1.5 shrink-0 rounded-full", connected ? "bg-ok" : "bg-warn")} />
            <span className={connected ? "text-ok" : "text-warn"}>{connected ? "实时连接" : "轮询连接"}</span>
            <span className="ml-auto tnum">v{liveVersion || version || "—"}</span>
          </p>
        </div>

        <nav aria-label="主导航" className="min-h-0 flex-1 overflow-y-auto hide-scrollbar">
          <ul className="space-y-1.5">
            {NAV.map((entry) => {
              const Icon = entry.icon;
              const isActive = active(entry.to);
              return (
                <li key={entry.to}>
                  <NavLink to={entry.to} className="block no-underline" aria-current={isActive ? "page" : undefined}>
                    {isActive ? (
                      <MovingBorder duration={9000} containerClassName="rounded-xl" colors={["#22d3ee", "#6392ff", "#a78bfa", "#22d3ee"]}>
                        <span className="flex items-center gap-3 rounded-xl bg-brand px-3.5 py-3 text-[0.88rem] font-semibold text-brand-ink shadow-card">
                          <Icon className="size-[1.05rem]" />
                          {entry.label}
                        </span>
                      </MovingBorder>
                    ) : (
                      <span className="nav-idle flex items-center gap-3 rounded-xl px-3.5 py-3 text-[0.88rem] text-soft hover:bg-sunken hover:text-ink">
                        <Icon className="size-[1.05rem]" />
                        {entry.label}
                      </span>
                    )}
                  </NavLink>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="mt-4 flex items-center justify-between border-t border-line pt-4">
          <div className="flex items-center gap-1">
            <AnimatedTooltip content="账户与安全">
              <AccountLink />
            </AnimatedTooltip>
            <AnimatedTooltip content="切换主题">
              <ThemeButton />
            </AnimatedTooltip>
          </div>
          <AnimatedTooltip content="退出登录">
            <LogoutButton />
          </AnimatedTooltip>
        </div>
      </div>
    </aside>
  );
}

function MobileTopBar() {
  const viewport = useViewport();
  const watch = viewport === "watch";

  return (
    <header
      className={cn(
        "glass-chrome relative z-20 flex shrink-0 items-center border-b border-line px-3 pt-safe",
        watch ? "h-11" : "h-14",
      )}
    >
      <BrandMark compact={watch} />
      <div className="min-w-0 flex-1" />
      <div className="flex shrink-0 items-center gap-1.5">
        <LiveDot />
        {!watch ? <ThemeButton /> : null}
      </div>
    </header>
  );
}

function MoreSheet({
  open,
  onOpenChange,
  active,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  active: (to: string) => boolean;
}) {
  const navigate = useNavigate();
  const logout = useAuth((state) => state.logout);
  const version = useAuth((state) => state.version);
  const liveVersion = useLive((state) => state.version);
  const { mode, set } = useTheme();

  const go = (to: string) => {
    onOpenChange(false);
    navigate(to);
  };

  const modeOptions: ThemeMode[] = ["system", "light", "dark"];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="sm" className="pb-6">
        <DialogHeader>
          <DialogTitle>更多</DialogTitle>
          <DialogDescription>系统状态、设置、账户与显示偏好</DialogDescription>
        </DialogHeader>

        <div className="space-y-1.5">
          {NAV.slice(4).map((entry) => {
            const Icon = entry.icon;
            return (
              <button
                key={entry.to}
                type="button"
                onClick={() => go(entry.to)}
                className={cn(
                  "tap-row flex w-full items-center gap-3 rounded-xl border px-3 text-left",
                  active(entry.to)
                    ? "border-transparent bg-brand text-brand-ink"
                    : "border-line bg-sunken text-ink",
                )}
              >
                <Icon className="size-4" />
                <span className="min-w-0 flex-1">
                  <span className="block text-[0.85rem] font-medium">{entry.label}</span>
                  <span className={cn("block text-[0.68rem]", active(entry.to) ? "text-brand-ink" : "text-faint")}>
                    {entry.hint}
                  </span>
                </span>
                <ChevronRight className="size-4 opacity-60" />
              </button>
            );
          })}
          <button
            type="button"
            onClick={() => go("/account")}
            className="tap-row flex w-full items-center gap-3 rounded-xl border border-line bg-sunken px-3 text-left text-ink"
          >
            <UserRound className="size-4" />
            <span className="min-w-0 flex-1">
              <span className="block text-[0.85rem] font-medium">账户与安全</span>
              <span className="block text-[0.68rem] text-faint">密码 · 二次验证</span>
            </span>
            <ChevronRight className="size-4 opacity-60" />
          </button>
        </div>

        <div className="rounded-xl border border-line bg-sunken p-3">
          <p className="eyebrow mb-2">主题</p>
          <div className="grid grid-cols-3 gap-1.5">
            {modeOptions.map((option) => {
              const Icon = THEME_ICON[option];
              return (
                <button
                  key={option}
                  type="button"
                  onClick={() => set(option)}
                  className={cn(
                    "tap-row flex flex-col items-center justify-center gap-1 rounded-lg border py-2 text-[0.7rem]",
                    mode === option
                      ? "border-transparent bg-brand text-brand-ink"
                      : "border-line bg-panel text-soft",
                  )}
                >
                  <Icon className="size-4" />
                  {THEME_LABEL[option].replace("主题：", "")}
                </button>
              );
            })}
          </div>
        </div>

        <div className="flex items-center justify-between rounded-xl border border-line bg-sunken px-3 py-2.5">
          <span className="tnum text-[0.72rem] text-faint">v{liveVersion || version || "—"}</span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              onOpenChange(false);
              void logout();
            }}
          >
            <LogOut className="size-4" />
            退出登录
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function BottomDock({ active }: { active: (to: string) => boolean }) {
  const viewport = useViewport();
  const watch = viewport === "watch";
  const [moreOpen, setMoreOpen] = useState(false);
  const items = NAV.filter((entry) => DOCK_ROUTES.includes(entry.to)).map((entry) => {
    const Icon = entry.icon;
    return {
      href: entry.to,
      label: entry.label,
      icon: <Icon />,
      active: active(entry.to),
    };
  });

  return (
    <>
      <FloatingDock
        compact={watch}
        className="ace-dock--floating"
        label="主导航"
        items={[
          ...items,
          {
            href: "#more",
            label: "更多",
            icon: <MoreHorizontal />,
            active: false,
            onClick: () => setMoreOpen(true),
          },
        ]}
      />
      <MoreSheet open={moreOpen} onOpenChange={setMoreOpen} active={active} />
    </>
  );
}

export function AppShell() {
  const viewport = useViewport();
  const active = useActivePath();
  const watch = viewport === "watch";
  const desktop = viewport === "desktop";
  const { pathname } = useLocation();
  const mainRef = useRef<HTMLElement | null>(null);

  // The content area is the scroll container (desktop and mobile both), so a
  // client-side route change does not reset window scroll. Reset it explicitly
  // or a deep page leaves the next page half-scrolled.
  useEffect(() => {
    mainRef.current?.scrollTo({ top: 0, behavior: "auto" });
  }, [pathname]);

  return (
    <div className="relative flex app-vh w-full flex-col overflow-hidden">
      {/* Flat ambient layer: a quiet engineering grid, no glow, no blur. */}
      <div className="pointer-events-none fixed top-0 right-0 bottom-0 left-0 z-0" aria-hidden="true">
        <GridPattern width={34} height={34} stroke="var(--c-grid-line)" />
      </div>

      <div className="relative z-10 flex min-h-0 w-full flex-1">
        {desktop ? <DesktopSidebar active={active} /> : null}

        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          {desktop ? null : <MobileTopBar />}

          <main ref={mainRef} className={cn("min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-contain", !desktop && "pb-dock")}>
            <div
              key={pathname}
              className={cn(
                "page-enter mx-auto w-full min-w-0",
                watch
                  ? "max-w-none px-2.5 py-3"
                  : desktop
                    ? "max-w-[1320px] px-6 py-6 xl:px-10 xl:py-8"
                    : "max-w-3xl px-4 py-5",
              )}
            >
              <Outlet />
            </div>
          </main>
        </div>
      </div>

      {desktop ? null : <BottomDock active={active} />}
    </div>
  );
}

