import { createHash } from "node:crypto";
import type { Plugin } from "vite";

/**
 * Serves a second stylesheet to engines that predate CSS cascade layers.
 *
 * The console has to open in the Android watch browser, which is Chromium 81.
 * Chrome 81 has no `@layer`, and an unknown at-rule is dropped *with its whole
 * block* — so every Tailwind rule, which all live inside `@layer`, would be
 * discarded and the page would render completely unstyled.
 *
 * Rather than flatten the main stylesheet (which would silently change cascade
 * semantics for everyone), this emits a flattened copy as a separate asset and
 * loads it only when the engine cannot do layers. Modern browsers get byte-for-
 * byte the same HTML and CSS as before, plus one `typeof` check.
 *
 * `@property` and `color-mix()` need no help here: Tailwind already emits its
 * own `@supports` fallbacks for both, and the guards are true on Chromium 81.
 */

const LAYER_STATEMENT = /@layer\s+[^{;]+;/g;

/** Replaces every `@layer name { ... }` with just its body, preserving order. */
function flattenLayers(css: string): string {
  let out = css.replace(LAYER_STATEMENT, "");

  for (;;) {
    const match = /@layer\s+[\w-]+\s*\{/.exec(out);
    if (!match) return out;

    const open = match.index + match[0].length - 1;
    let depth = 0;
    let close = -1;
    for (let i = open; i < out.length; i++) {
      if (out[i] === "{") depth++;
      else if (out[i] === "}") {
        depth--;
        if (depth === 0) {
          close = i;
          break;
        }
      }
    }
    if (close === -1) return out; // unbalanced; leave the rest untouched

    const body = out.slice(open + 1, close);
    out = out.slice(0, match.index) + body + out.slice(close + 1);
  }
}

export function legacyCss(): Plugin {
  let compatHref = "";

  return {
    name: "observatory:legacy-css",
    apply: "build",

    generateBundle(_options, bundle) {
      const cssAsset = Object.values(bundle).find(
        (f) => f.type === "asset" && f.fileName.endsWith(".css"),
      );
      if (!cssAsset || cssAsset.type !== "asset") return;

      const source = String(cssAsset.source);
      const flattened = flattenLayers(source);
      if (flattened === source) return;

      const hash = createHash("sha256").update(flattened).digest("hex").slice(0, 8);
      const fileName = `assets/compat-${hash}.css`;
      compatHref = `/${fileName}`;

      this.emitFile({
        type: "asset",
        fileName,
        source: flattened,
      });
    },

    transformIndexHtml(html) {
      if (!compatHref) return html;
      return {
        html,
        tags: [
          {
            tag: "script",
            injectTo: "head",
            children: `(function(){if(typeof CSSLayerBlockRule!=="undefined")return;var d=document.querySelector('link[rel="stylesheet"]');if(d)d.disabled=true;var l=document.createElement("link");l.rel="stylesheet";l.href=${JSON.stringify(
              compatHref,
            )};document.head.appendChild(l);})();`,
          },
        ],
      };
    },
  };
}
