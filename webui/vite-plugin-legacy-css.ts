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

/**
 * Chromium 81 flex-gap fallback generator.
 *
 * Chrome 81 supports `gap` only inside grid (since 57); flexbox got it in 84.
 * There is no `@supports` query that can tell the two apart (`@supports (gap:
 * 1rem)` is true on 81 — the property parses), so the fallback has to be
 * unconditional — which is why it is emitted only into the *compat* sheet that
 * pre-layer engines load.
 *
 * For every `gap-*` utility actually present in the build we emit sibling
 * margins on flex children (`* + *` keeps the first child flush), scoped to
 * `.flex`/`.inline-flex` containers so grid layouts — where `gap` already
 * works — are untouched:
 *
 *   .flex.gap-3        (row)      → children margin-left
 *   .flex.flex-col.gap-3          → children margin-top
 *   .gap-x- / .gap-y-  map to the same two directions
 *
 * Known trade-offs (documented rather than solved):
 *  * specificity (0,2,0) beats single-class child margins like `.ml-2` in the
 *    same direction — rare, and only on the watch;
 *  * wrapped rows: `gap-y` on a wrapping row-flex approximates as margin-top
 *    on every non-first child, which also adds it to the first row's children.
 *  * `flex-col-reverse` is skipped (would need margin-bottom instead).
 */
function flexGapFallback(css: string): string {
  const rowGapByClass = new Map<string, string>(); // gap-N   → both axes
  const colGapByClass = new Map<string, string>(); // gap-x-N → horizontal
  const verGapByClass = new Map<string, string>(); // gap-y-N → vertical

  const valueOf = (raw: string) => raw.trim().replace(/;$/, "");

  // Variants (`sm:gap-4` → `.sm\:gap-4`) intentionally never match: their
  // escaped class name breaks the `[a-z0-9.-]` run, and the watch viewport
  // never satisfies their min-width media queries anyway.
  const omnidir =
    /\.(gap-(?:\d+(?:\.\d+)?))\{[^{}]*?(?<![-\w])gap:\s*([^;}]+)/g;
  const colDir =
    /\.((?:gap-x-)(?:\d+(?:\.\d+)?))\{[^{}]*?column-gap:\s*([^;}]+)/g;
  const verDir =
    /\.((?:gap-y-)(?:\d+(?:\.\d+)?))\{[^{}]*?row-gap:\s*([^;}]+)/g;

  for (const re of [omnidir, colDir, verDir]) {
    re.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = re.exec(css))) {
      const store =
        re === omnidir ? rowGapByClass : re === colDir ? colGapByClass : verGapByClass;
      if (!store.has(m[1])) store.set(m[1], valueOf(m[2]));
    }
  }
  if (!rowGapByClass.size && !colGapByClass.size && !verGapByClass.size) {
    return "";
  }

  const rules: string[] = [];
  const flexRow = (cls: string, val: string, prop: string) => {
    rules.push(
      `.flex.${cls}:not(.flex-col):not(.flex-col-reverse)>*+*{${prop}:${val}}`,
      `.inline-flex.${cls}:not(.flex-col):not(.flex-col-reverse)>*+*{${prop}:${val}}`,
    );
  };
  const flexCol = (cls: string, val: string) => {
    rules.push(
      `.flex.flex-col.${cls}>*+*{margin-top:${val}}`,
      `.inline-flex.flex-col.${cls}>*+*{margin-top:${val}}`,
    );
  };

  for (const [cls, val] of rowGapByClass) {
    flexRow(cls, val, "margin-left");
    flexCol(cls, val);
  }
  for (const [cls, val] of colGapByClass) flexRow(cls, val, "margin-left");
  // Vertical rhythm on a wrapping row-flex: applied to non-first children
  // (approximation — the first wrapped row's children also get it).
  for (const [cls, val] of verGapByClass) {
    flexCol(cls, val);
    flexRow(cls, val, "margin-top");
  }

  return `\n/* flex-gap fallback for pre-84 engines (watch WebView) */\n${rules.join("\n")}\n`;
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

      const compat = flattened + flexGapFallback(source);

      const hash = createHash("sha256").update(compat).digest("hex").slice(0, 8);
      const fileName = `assets/compat-${hash}.css`;
      compatHref = `/${fileName}`;

      this.emitFile({
        type: "asset",
        fileName,
        source: compat,
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
