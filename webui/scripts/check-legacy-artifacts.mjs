/**
 * Artifact-level Chromium 81 check.
 *
 * The watch WebView is Chromium 81 and cannot be exercised in CI, so this
 * script inspects what actually ships: the generated CSS (main + compat),
 * the minified JS and the HTML loader. It fails on syntax that Chromium 81
 * cannot parse and that the compat pipeline is supposed to have removed.
 *
 *   node scripts/check-legacy-artifacts.mjs
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DIST = path.join(ROOT, "dist");

const errors = [];
const warnings = [];

function read(rel) {
  return fs.readFileSync(path.join(DIST, rel), "utf8");
}

if (!fs.existsSync(DIST)) {
  console.error("dist/ 不存在：先运行 pnpm build");
  process.exit(1);
}

const assets = fs.readdirSync(path.join(DIST, "assets"));
const cssFiles = assets.filter((name) => name.endsWith(".css"));
const jsFiles = assets.filter((name) => name.endsWith(".js"));
const html = fs.readFileSync(path.join(DIST, "index.html"), "utf8");

const mainCssName = cssFiles.find((name) => !name.startsWith("compat-"));
const compatCssName = cssFiles.find((name) => name.startsWith("compat-"));

if (!mainCssName) errors.push("没有找到主样式表");
if (!compatCssName) errors.push("没有找到 compat 样式表（legacy-css 插件未产出）");
if (jsFiles.length === 0) errors.push("没有找到 JS 产物");

if (!html.includes("CSSLayerBlockRule")) {
  errors.push("index.html 缺少 @layer 探测加载器（旧引擎不会切到 compat 样式表）");
}
if (!html.includes(compatCssName)) {
  errors.push("index.html 没有引用 compat 样式表");
}

const count = (text, needle) => text.split(needle).length - 1;

// ---------------------------------------------------------------- main CSS ---
if (mainCssName) {
  const css = read(path.join("assets", mainCssName));

  // Properties Chromium 81 does not understand at all. A declaration is
  // ignored, but a layout built only from it collapses — treat as errors.
  for (const property of ["translate:", "rotate:", "scale:"]) {
    if (new RegExp(`[;{]${property}`).test(css)) {
      errors.push(`主样式表仍含 ${property} 声明（Chromium 81 不支持）；改用 transform`);
    }
  }
  if (/[;{]inset:/.test(css)) {
    warnings.push("主样式表含 inset 简写（Chromium 81 忽略；compat 样式表会展开为物理属性）");
  }
  if (count(css, "oklch(") > 0) errors.push("主样式表仍含 oklch()");
  if (count(css, "@starting-style") > 0) errors.push("主样式表仍含 @starting-style");
  if (count(css, "@container") > 0) {
    warnings.push("主样式表含 @container（Chromium 81 忽略，确认不承载关键布局）");
  }
  if (count(css, "color-mix(") > 0) {
    warnings.push(`主样式表含 ${count(css, "color-mix(")} 处 color-mix()（兼容声明需有静态回退）`);
  }
  if (count(css, ":has(") > 0) {
    warnings.push(`主样式表含 ${count(css, ":has(")} 处 :has()（Chromium 81 丢弃这些规则，已确认仅装饰）`);
  }
  if (!/\.ratio-16-10:{1,2}before/.test(css)) {
    errors.push("主样式表缺少 .ratio-* 的 aspect-ratio 回退");
  }
  if (!css.includes("@supports (height: 100dvh)")) {
    errors.push("主样式表缺少 100dvh 回退");
  }
}

// -------------------------------------------------------------- compat CSS ---
if (compatCssName) {
  const css = read(path.join("assets", compatCssName));

  if (count(css, "@layer") > 0) errors.push("compat 样式表仍含 @layer（旧引擎会整块丢弃）");
  for (const property of [
    "padding-inline:",
    "padding-block:",
    "margin-inline:",
    "margin-block:",
    "inset-inline:",
    "inset-block:",
  ]) {
    if (count(css, property) > 0) {
      errors.push(`compat 样式表仍含逻辑属性简写 ${property}（应已改写为物理属性）`);
    }
  }
  if (/[;{]translate:/.test(css)) errors.push("compat 样式表仍含 translate 属性");
  if (/[;{]inset:/.test(css)) errors.push("compat 样式表仍含 inset 简写（应已展开）");
  if (!css.includes("flex-gap fallback")) {
    warnings.push("compat 样式表没有 flex-gap 回退（若页面不使用 flex gap 可忽略）");
  }
  if (count(css, "color-mix(") > 0) {
    warnings.push(`compat 样式表含 ${count(css, "color-mix(")} 处 color-mix()（仅非关键声明）`);
  }
}

// --------------------------------------------------------------------- JS ---
for (const jsFile of jsFiles) {
  const js = read(path.join("assets", jsFile));

  // Syntax added after Chromium 81. Babel must have lowered these.
  if (/\?\?=/.test(js)) errors.push(`${jsFile}: 仍含 ??= 赋值`);
  if (/\|\|=/.test(js)) errors.push(`${jsFile}: 仍含 ||= 赋值`);
  if (/&&=/.test(js)) errors.push(`${jsFile}: 仍含 &&= 赋值`);
  if (/static\s*\{/.test(js)) errors.push(`${jsFile}: 仍含 class static block`);
  if (/#[A-Za-z_$][\w$]*\s*\(/.test(js)) errors.push(`${jsFile}: 仍含私有方法语法`);

  // Runtime APIs Chromium 81 lacks. They must be shimmed or unused.
  if (js.includes("structuredClone") && !js.includes("structuredClone=") && !js.includes("structuredClone =")) {
    warnings.push(`${jsFile}: 使用了 structuredClone（确认 compat.ts 已加载）`);
  }
  if (js.includes("Object.hasOwn") && !js.includes("hasOwn")) {
    warnings.push(`${jsFile}: 使用了 Object.hasOwn（确认 compat.ts 已加载）`);
  }
  if (js.includes("ResizeObserver") && !js.includes("ResizeObserver")) {
    warnings.push(`${jsFile}: 使用了 ResizeObserver（Chromium 64+，81 已支持）`);
  }
}

// ------------------------------------------------------------------ report ---
const rel = (name) => `dist/assets/${name}`;
console.log("Chromium 81 artifact check");
console.log(`  main CSS : ${mainCssName ? rel(mainCssName) : "(missing)"}`);
console.log(`  compat   : ${compatCssName ? rel(compatCssName) : "(missing)"}`);
console.log(`  JS       : ${jsFiles.map(rel).join(", ") || "(missing)"}`);

for (const warning of warnings) console.warn(`  WARN  ${warning}`);
for (const error of errors) console.error(`  FAIL  ${error}`);

if (errors.length > 0) {
  console.error(`\n${errors.length} 项兼容性检查失败`);
  process.exit(1);
}
console.log(`\n通过（${warnings.length} 条提示）`);
