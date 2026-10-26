/**
 * Application shell.
 *
 * Three navigation variants over one router:
 *  - desktop: a quiet sidebar (brand, two labelled nav groups, status footer);
 *  - mobile: a compact top bar plus a bottom dock (four destinations + "more");
 *  - watch: the same dock, icon-only and tighter.
 *
 * The chrome is one hairline border and a value step; it never competes with the
 * page content. No decorative layers, no coloured active pill.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
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
} from "lucide-react";

import { Button, buttonVariants } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
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

const NAV_GROUPS: Array<{ label: string; items: NavEntry[] }> = [
  {
    label: "监控",
    items: [
      { to: "/wall", label: "设备墙", hint: "全部受控端", icon: MonitorSmartphone },
      { to: "/backup", label: "归档", hint: "远端备份", icon: Archive },
      { to: "/updates", label: "更新", hint: "受控端版本", icon: PackageCheck },
      { to: "/audit", label: "审计", hint: "操作留痕", icon: ScrollText },
    ],
  },
  {
    label: "管理",
    items: [
      { to: "/system", label: "系统", hint: "服务端状态", icon: ServerCog },
      { to: "/settings", label: "设置", hint: "全局配置", icon: SettingsIcon },
    ],
  },
];

const NAV = NAV_GROUPS.flatMap((group) => group.items);

/** The four most-used destinations live in the dock; the rest behind "more". */
const DOCK_ROUTES = ["/wall", "/backup", "/updates", "/audit"];

const THEME_ICON: Record<ThemeMode, Icon> = {
  system: SunMoon,
  light: Sun,
  dark: Moon,
};

function useActivePath() {
  const { pathname } = useLocation();
  return useCallback(
    (to: string) =>
      pathname === to ||
      pathname.startsWith(`${to}/`) ||
      (to === "/wall" && pathname.startsWith("/device/")),
    [pathname],
  );
}

function LiveDot({ className }: { className?: string }) {
  const connected = useLive((state) => state.connected);
  const version = useLive((state) => state.version);
  const authVersion = useAuth((state) => state.version);
  return (
    <div className={cn("flex min-w-0 items-center gap-1.5 text-xs", className)}>
      <span
        className={cn("size-1 shrink-0 rounded-full", connected ? "bg-success" : "bg-warning")}
        aria-hidden
      />
      <span className={connected ? "text-muted-foreground" : "text-warning"}>
        {connected ? "实时" : "轮询"}
      </span>
      <span className="ml-auto tnum shrink-0 text-faint">v{version || authVersion || "—"}</span>
    </div>
  );
}

function BrandMark({ compact = false }: { compact?: boolean }) {
  return (
    <div className="flex min-w-0 items-center gap-3">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border text-xs font-bold">
        Ob
      </div>
      {compact ? null : (
        <div className="flex min-w-0 flex-col">
          <span className="truncate text-sm leading-tight font-semibold">Observatory</span>
          <span className="mt-0.5 truncate text-[10px] tracking-widest text-muted-foreground uppercase">
            Mission Control
          </span>
        </div>
      )}
    </div>
  );
}

function IconAction({
  label,
  onClick,
  children,
  className,
}: {
  label: string;
  onClick?: () => void;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <Tooltip>
      <TooltipTrigger
        render={
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={label}
            onClick={onClick}
            className={className}
          />
        }
      >
        {children}
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

function ThemeButton() {
  const { mode, cycle } = useTheme();
  const Icon = THEME_ICON[mode];
  return (
    <IconAction label={THEME_LABEL[mode]} onClick={cycle} className="size-8 rounded-md p-2">
      <Icon className="size-4 stroke-[1.5]" />
    </IconAction>
  );
}

function LogoutButton({ className }: { className?: string }) {
  const logout = useAuth((state) => state.logout);
  return (
    <IconAction
      label="退出登录"
      onClick={() => void logout()}
      className={cn("size-8 rounded-md p-2", className)}
    >
      <LogOut className="size-4 stroke-[1.5]" />
    </IconAction>
  );
}

function NavItem({
  entry,
  active,
}: {
  entry: NavEntry;
  active: (to: string) => boolean;
}) {
  const Icon = entry.icon;
  const isActive = active(entry.to);
  return (
    <li>
      <NavLink
        to={entry.to}
        aria-current={isActive ? "page" : undefined}
        className={cn(
          "flex items-center gap-3 rounded-md px-2 py-2 text-sm no-underline transition-colors",
          isActive
            ? "relative bg-secondary font-medium text-secondary-foreground"
            : "text-muted-foreground hover:bg-muted",
        )}
      >
        {isActive ? (
          <span
            className="absolute top-2 left-0 h-4 w-1 rounded-r bg-foreground"
            aria-hidden
          />
        ) : null}
        <Icon
          className={cn(
            "h-4 w-4 shrink-0 stroke-[1.5]",
            isActive ? "text-foreground" : "text-muted-foreground",
          )}
          aria-hidden
        />
        <span className="truncate">{entry.label}</span>
      </NavLink>
    </li>
  );
}

function SidebarStatus() {
  const connected = useLive((state) => state.connected);
  const version = useLive((state) => state.version);
  const authVersion = useAuth((state) => state.version);
  return (
    <div className="flex items-center justify-between px-4 py-3 text-xs text-muted-foreground">
      <div className="flex items-center gap-1.5">
        <span
          className={cn("h-1.5 w-1.5 rounded-full", connected ? "bg-success" : "bg-warning")}
          aria-hidden
        />
        <span>{connected ? "实时" : "轮询"}</span>
      </div>
      <span className="font-mono text-[10px]">v{version || authVersion || "—"}</span>
    </div>
  );
}

function DesktopSidebar({ active }: { active: (to: string) => boolean }) {
  return (
    <aside className="hidden w-[240px] shrink-0 flex-col border-r border-border bg-background lg:flex">
      <div className="flex items-center gap-3 px-4 py-5">
        <BrandMark />
      </div>

      <nav aria-label="主导航" className="min-h-0 flex-1 overflow-y-auto hide-scrollbar px-3 pb-4">
        {NAV_GROUPS.map((group, index) => (
          <div key={group.label} className={index > 0 ? "mt-6" : undefined}>
            <div className="px-2 py-2 text-xs font-semibold text-muted-foreground">{group.label}</div>
            <ul className="flex flex-col gap-0.5">
              {group.items.map((entry) => (
                <NavItem key={entry.to} entry={entry} active={active} />
              ))}
            </ul>
          </div>
        ))}
      </nav>

      <div className="mt-auto flex flex-col border-t">
        <SidebarStatus />

        <div className="flex items-center justify-between px-4 pb-4">
          <Tooltip>
            <TooltipTrigger
              render={
                <NavLink
                  to="/account"
                  aria-label="账户与安全"
                  className={cn(
                    buttonVariants({ variant: "ghost", size: "icon-sm" }),
                    "size-8 rounded-md p-2 no-underline",
                  )}
                />
              }
            >
              <UserRound className="size-4 stroke-[1.5]" />
            </TooltipTrigger>
            <TooltipContent>账户与安全</TooltipContent>
          </Tooltip>
          <ThemeButton />
          <LogoutButton />
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
        "relative z-20 flex shrink-0 items-center border-b border-border bg-background px-3 pt-safe",
        watch ? "h-11" : "h-12",
      )}
    >
      <BrandMark compact={watch} />
      <div className="min-w-0 flex-1" />
      <div className="flex shrink-0 items-center gap-1.5">
        <LiveDot className="mr-1" />
        {watch ? null : <ThemeButton />}
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
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="bottom" className="pb-[calc(1rem+env(safe-area-inset-bottom))]">
        <SheetHeader>
          <SheetTitle>更多</SheetTitle>
          <SheetDescription>系统状态、设置、账户与显示偏好</SheetDescription>
        </SheetHeader>

        <div className="flex flex-col gap-0.5 px-3">
          {NAV.slice(4).map((entry) => {
            const Icon = entry.icon;
            const isActive = active(entry.to);
            return (
              <button
                key={entry.to}
                type="button"
                onClick={() => go(entry.to)}
                className={cn(
                  "tap-row flex w-full items-center gap-3 rounded-md px-3 text-left transition-colors",
                  isActive ? "bg-accent text-foreground" : "text-foreground hover:bg-accent",
                )}
              >
                <Icon className="size-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium">{entry.label}</span>
                  <span className="block text-xs text-muted-foreground">{entry.hint}</span>
                </span>
                <ChevronRight className="size-4 shrink-0 text-faint" />
              </button>
            );
          })}
          <button
            type="button"
            onClick={() => go("/account")}
            className="tap-row flex w-full items-center gap-3 rounded-md px-3 text-left text-foreground hover:bg-accent"
          >
            <UserRound className="size-4 shrink-0 text-muted-foreground" />
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-medium">账户与安全</span>
              <span className="block text-xs text-muted-foreground">密码 · 二次验证</span>
            </span>
            <ChevronRight className="size-4 shrink-0 text-faint" />
          </button>
        </div>

        <div className="px-3">
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
                    "tap-row flex flex-col items-center justify-center gap-1 rounded-md border py-2 text-xs transition-colors",
                    mode === option
                      ? "border-foreground bg-foreground text-background"
                      : "border-border bg-card text-muted-foreground hover:bg-accent",
                  )}
                >
                  <Icon className="size-4" />
                  {THEME_LABEL[option].replace("主题：", "")}
                </button>
              );
            })}
          </div>
        </div>

        <div className="mx-3 flex items-center justify-between border-t border-border pt-3">
          <span className="tnum text-xs text-faint">
            v{liveVersion || version || "—"}
          </span>
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
      </SheetContent>
    </Sheet>
  );
}

function BottomDock({ active }: { active: (to: string) => boolean }) {
  const viewport = useViewport();
  const watch = viewport === "watch";
  const [moreOpen, setMoreOpen] = useState(false);

  return (
    <>
      <nav
        aria-label="主导航"
        className="fixed right-0 bottom-0 left-0 z-20 flex items-stretch border-t border-border bg-background pb-safe"
      >
        {NAV.filter((entry) => DOCK_ROUTES.includes(entry.to)).map((entry) => {
          const Icon = entry.icon;
          const isActive = active(entry.to);
          return (
            <NavLink
              key={entry.to}
              to={entry.to}
              aria-current={isActive ? "page" : undefined}
              className={cn(
                "flex min-w-0 flex-1 flex-col items-center justify-center gap-0.5 no-underline transition-colors",
                watch ? "min-h-11 py-1" : "min-h-14 py-1.5",
                isActive
                  ? "font-medium text-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              <Icon className={cn(watch ? "size-4" : "size-[1.15rem]")} aria-hidden />
              <span className={cn("truncate", watch ? "text-[0.58rem]" : "text-xs")}>
                {entry.label}
              </span>
            </NavLink>
          );
        })}
        <button
          type="button"
          onClick={() => setMoreOpen(true)}
          aria-label="更多"
          className={cn(
            "flex min-w-0 flex-1 flex-col items-center justify-center gap-0.5 text-muted-foreground transition-colors hover:text-foreground",
            watch ? "min-h-11 py-1" : "min-h-14 py-1.5",
          )}
        >
          <MoreHorizontal className={cn(watch ? "size-4" : "size-[1.15rem]")} aria-hidden />
          <span className={cn("truncate", watch ? "text-[0.58rem]" : "text-xs")}>更多</span>
        </button>
      </nav>
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
    <div className="relative flex app-vh w-full flex-col overflow-hidden bg-background">
      <div className="relative z-10 flex min-h-0 w-full flex-1">
        {desktop ? <DesktopSidebar active={active} /> : null}

        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          {desktop ? null : <MobileTopBar />}

          <main
            ref={mainRef}
            className={cn(
              "min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-contain",
              desktop ? null : "pb-dock",
            )}
          >
            <div
              key={pathname}
              className={cn(
                "page-enter mx-auto w-full min-w-0",
                watch
                  ? "max-w-none px-2.5 pt-3 pb-4"
                  : desktop
                    ? "max-w-[1360px] px-8 pt-7 pb-10 xl:px-10"
                    : "max-w-3xl px-4 pt-5 pb-8",
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
