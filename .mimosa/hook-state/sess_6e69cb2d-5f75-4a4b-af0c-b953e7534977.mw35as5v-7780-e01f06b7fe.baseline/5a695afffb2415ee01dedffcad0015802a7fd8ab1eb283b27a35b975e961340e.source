import { transformSync } from "@babel/core";
import type { Plugin } from "vite";

/**
 * Lowers the bundle to what Chromium 81 can actually parse.
 *
 * The watch browser is Chromium 81, which cannot parse `??=` / `||=` / `&&=`
 * (Chromium 85) or private methods `#m(){}` (Chromium 84). Either one is a hard
 * SyntaxError, so the page never renders at all — this is what made the console
 * a blank screen on the watch.
 *
 * esbuild is deliberately NOT used for this. Lowering private methods with
 * esbuild silently breaks Base UI's dropdown menus (verified: the menu stops
 * opening with no error), so the build keeps `target: es2022` and this runs
 * last, after minification, using Babel's spec-correct lowering.
 *
 * Only syntax is lowered. Polyfills are not injected here — the few missing
 * runtime APIs are shimmed in `src/compat.ts` instead.
 */
export function legacyJs(): Plugin {
  return {
    name: "observatory:legacy-js",
    apply: "build",
    enforce: "post",

    renderChunk(code) {
      const result = transformSync(code, {
        // No config discovery: this must be deterministic inside the build.
        babelrc: false,
        configFile: false,
        sourceType: "module",
        compact: true,
        comments: false,
        presets: [
          [
            "@babel/preset-env",
            {
              targets: { chrome: "81" },
              modules: false,
              // Keep polyfills out of the bundle; compat.ts owns that.
              useBuiltIns: false,
            },
          ],
        ],
      });
      if (!result?.code) return null;
      return { code: result.code, map: null };
    },
  };
}
