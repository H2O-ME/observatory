import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

import { legacyCss } from "./vite-plugin-legacy-css";
import { legacyJs } from "./vite-plugin-legacy-js";

// The console is served by the API container itself (data_dir/webui), so the
// production build talks to the same origin. `pnpm dev` proxies to a local
// or remote server instead, which keeps a single code path for API URLs.
export default defineConfig({
  plugins: [react(), tailwindcss(), legacyCss(), legacyJs()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  build: {
    outDir: "dist",
    // Frame thumbnails are the bulk of the traffic and are immutable; nothing
    // here needs a service worker or legacy transpilation.
    //
    // Stays es2022 deliberately. Do not lower it to reach the watch browser:
    // esbuild's private-method lowering silently breaks Base UI's dropdowns.
    // The Chromium 81 syntax floor is handled by the Babel pass in
    // vite-plugin-legacy-js.ts, which runs after minification.
    target: "es2022",
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.OBS_DEV_SERVER ?? "https://127.0.0.1:8443",
        changeOrigin: true,
        secure: false,
        ws: true,
      },
    },
  },
  // `pnpm preview` serves the minified build through the same proxy, so the
  // artifact that actually ships can be exercised against a real server without
  // having to trust its (usually self-signed) certificate in the browser.
  preview: {
    port: 4173,
    proxy: {
      "/api": {
        target: process.env.OBS_DEV_SERVER ?? "https://127.0.0.1:8443",
        changeOrigin: true,
        secure: false,
        ws: true,
      },
    },
  },
});
