/// <reference types="vite/client" />

/**
 * The pre-paint theme bridge.
 *
 * `index.html` resolves the theme before the first paint (so dark-mode operators
 * never see a white flash) and hands the *mode* over here. The React shell reads
 * this value instead of re-deriving it, which keeps one decision for one frame.
 */
declare global {
  interface Window {
    __OBS_THEME_MODE__?: "system" | "light" | "dark";
  }
}

export {};