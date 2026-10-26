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
 * The compat sheet also rewrites `padding-inline`/`padding-block`/`margin-*`/
 * `inset-*` shorthands into physical longhands. Tailwind v4 emits those for
 * `px-*`, `py-*`, `mx-*` and `my-*`, and Chromium 81 only has the `-start`/
 * `-end` longhands (not the shorthands, added in 87) — without this the watch
 * would render every padding/margin rule as a no-op.
 *
 * Alpha colours are not synthesised here: the stylesheet simply does not use
 * `color-mix()` for layout-critical values (tokens are opaque, status tints are
 * `*-soft` tokens). The only remaining `color-mix()` is Tailwind's placeholder
 * preflight, which `index.css` overrides with an opaque colour.
 */

const LAYER_STATEMENT = /@layer\s+[^{;]+;/g;

/** Splits a CSS value on top-level whitespace (parens keep their spaces). */
function splitValue(value: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let start = 0;

  for (let i = 0; i < value.length; i++) {
    const char = value[i];
    if (char === "(") depth++;
    else if (char === ")") depth--;
    else if (depth === 0 && (char === " " || char === "\t" || char === "\n")) {
      if (i > start) out.push(value.slice(start, i));
      start = i + 1;
      while (start < value.length && /\s/.test(value[start])) start++;
      i = start - 1;
    }
  }
  if (start < value.length) out.push(value.slice(start));
  return out;
}

/** Two-axis logical shorthands → the physical pair (LTR, which is all we ship). */
const LOGICAL_AXES: Record<string, [string, string]> = {
  "padding-inline": ["padding-left", "padding-right"],
  "padding-block": ["padding-top", "padding-bottom"],
  "margin-inline": ["margin-left", "margin-right"],
  "margin-block": ["margin-top", "margin-bottom"],
  "inset-inline": ["left", "right"],
  "inset-block": ["top", "bottom"],
  "scroll-margin-inline": ["scroll-margin-left", "scroll-margin-right"],
  "scroll-margin-block": ["scroll-margin-top", "scroll-margin-bottom"],
  "scroll-padding-inline": ["scroll-padding-left", "scroll-padding-right"],
  "scroll-padding-block": ["scroll-padding-top", "scroll-padding-bottom"],
};

/** Single logical longhands → physical. */
const LOGICAL_SINGLE: Record<string, string> = {
  "padding-inline-start": "padding-left",
  "padding-inline-end": "padding-right",
  "padding-block-start": "padding-top",
  "padding-block-end": "padding-bottom",
  "margin-inline-start": "margin-left",
  "margin-inline-end": "margin-right",
  "margin-block-start": "margin-top",
  "margin-block-end": "margin-bottom",
  "inset-inline-start": "left",
  "inset-inline-end": "right",
  "inset-block-start": "top",
  "inset-block-end": "bottom",
  "border-inline-start-width": "border-left-width",
  "border-inline-end-width": "border-right-width",
  "border-block-start-width": "border-top-width",
  "border-block-end-width": "border-bottom-width",
  "inline-size": "width",
  "block-size": "height",
  "min-inline-size": "min-width",
  "max-inline-size": "max-width",
  "min-block-size": "min-height",
  "max-block-size": "max-height",
};

function logicalToPhysical(css: string): string {
  let out = css;

  out = out.replace(
    /(^|[;{])\s*((?:padding|margin|inset|scroll-margin|scroll-padding)-(?:inline|block))\s*:\s*([^;}]+)/g,
    (_match, prefix: string, property: string, raw: string) => {
      const pair = LOGICAL_AXES[property];
      if (!pair) return `${prefix}${property}:${raw}`;
      const values = splitValue(raw.trim());
      const first = values[0] ?? "0";
      const second = values[1] ?? first;
      return `${prefix}${pair[0]}:${first};${pair[1]}:${second}`;
    },
  );

  out = out.replace(
    /(^|[;{])\s*((?:padding|margin|inset|border)-(?:inline|block)-(?:start|end)(?:-width)?|(?:min-|max-)?(?:inline|block)-size)\s*:\s*([^;}]+)/g,
    (_match, prefix: string, property: string, raw: string) => {
      const physical = LOGICAL_SINGLE[property];
      return physical ? `${prefix}${physical}:${raw}` : `${prefix}${property}:${raw}`;
    },
  );

  // `inset` shorthand is Chromium 87. The CSS minifier rewrites our physical
  // four-property fallbacks back into it, so expand it again here.
  out = out.replace(
    /(^|[;{])\s*inset\s*:\s*([^;}]+)/g,
    (_match, prefix: string, raw: string) => {
      const values = splitValue(raw.trim());
      const [top, right = top, bottom = top, left = right] = values;
      return `${prefix}top:${top};right:${right};bottom:${bottom};left:${left}`;
    },
  );

  return out;
}


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

      const compat = logicalToPhysical(flattened + flexGapFallback(source));

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
