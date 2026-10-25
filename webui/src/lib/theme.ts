import { useCallback, useSyncExternalStore } from "react";

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

/**
 * Module-level theme store.
 *
 * The old per-hook `useState` meant every `useTheme()` instance (top-bar
 * button, mobile "more" sheet, login page switcher) kept its own copy and
 * drifted apart after the first toggle. The store keeps one source of truth;
 * `useSyncExternalStore` subscribes every instance to it. The system-preference
 * listener also lives here once, not once per consumer.
 */
const listeners = new Set<() => void>();
let currentMode: ThemeMode =
  (globalThis as { __OBS_THEME_MODE__?: ThemeMode }).__OBS_THEME_MODE__ ?? readStoredMode();

function notify(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function setMode(next: ThemeMode): void {
  if (next === currentMode) return;
  currentMode = next;
  applyTheme(next);
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch {
    /* storage disabled: the choice still applies for this page */
  }
  notify();
}

// One global listener for `system` mode: applies and notifies even when no
// component happens to be mounted in that mode.
try {
  const query = window.matchMedia("(prefers-color-scheme: dark)");
  query.addEventListener("change", () => {
    if (currentMode === "system") {
      applyTheme("system");
      notify();
    }
  });
} catch {
  /* matchMedia unavailable: system mode falls back to light */
}

applyTheme(currentMode);

export function useTheme() {
  const mode = useSyncExternalStore(subscribe, () => currentMode);

  const cycle = useCallback(() => {
    setMode(
      currentMode === "system" ? "light" : currentMode === "light" ? "dark" : "system",
    );
  }, []);

  const set = useCallback((next: ThemeMode) => setMode(next), []);

  return { mode, cycle, set };
}
