import { useCallback, useEffect, useState } from "react";

const THEME_KEY = "observatory.theme";

export type ThemeMode = "system" | "light" | "dark";

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
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  } catch {
    return false;
  }
}

export function resolveDark(mode: ThemeMode): boolean {
  return mode === "dark" || (mode === "system" && prefersDark());
}

function applyTheme(mode: ThemeMode): void {
  const dark = resolveDark(mode);
  const root = document.documentElement;
  root.classList.toggle("dark", dark);
  root.style.colorScheme = dark ? "dark" : "light";
}

export const THEME_LABEL: Record<ThemeMode, string> = {
  system: "主题：跟随系统",
  light: "主题：浅色",
  dark: "主题：深色",
};

export function useTheme() {
  const [mode, setMode] = useState<ThemeMode>(
    () => window.__OBS_THEME_MODE__ ?? readStoredMode(),
  );

  useEffect(() => {
    applyTheme(mode);
    try {
      localStorage.setItem(THEME_KEY, mode);
    } catch {
      /* storage disabled: the choice still applies for this page */
    }
    if (mode !== "system") return;
    let query: MediaQueryList | null = null;
    try {
      query = window.matchMedia("(prefers-color-scheme: dark)");
    } catch {
      return;
    }
    const onChange = () => applyTheme("system");
    query.addEventListener("change", onChange);
    return () => query?.removeEventListener("change", onChange);
  }, [mode]);

  const cycle = useCallback(() => {
    setMode((current) => (current === "system" ? "light" : current === "light" ? "dark" : "system"));
  }, []);

  const set = useCallback((next: ThemeMode) => setMode(next), []);

  return { mode, cycle, set };
}
