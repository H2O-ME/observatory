# Observatory WebUI · Design System（shadcn/ui）

> 本文件是 WebUI 的视觉与组件契约。改页面之前先读这里；
> `src/components/ui/` 是唯一的组件体系，页面只组合、不重造。

## 1. 体系

- **唯一设计体系**：shadcn/ui（base-nova 风格，Base UI 基元）。
- 组件源码在仓库内维护（`src/components/ui/`），不依赖运行时远程加载。
- 已移除：`@radix-ui/themes`、`@base-ui` 之外的其它 UI 库、Aceternity 装饰组件、
  `framer-motion`、`recharts`（未使用）、以及对应的 CSS。
- 组件实现来自官方 registry（`https://ui.shadcn.com/r/styles/base-nova/*`），
  本地只做两类改动：
  1. 令牌映射到本项目的语义色；
  2. Chromium 81 兼容替换（见 §5）。

## 2. 令牌

所有颜色、圆角、字体集中在 `src/index.css`。页面不得硬编码色值。

语义令牌（`@theme inline` 映射）：

| 令牌 | 用途 |
| --- | --- |
| `background` / `foreground` | 页面底色与主文字 |
| `card` / `card-foreground` | 面板 |
| `popover` / `popover-foreground` | 弹层、下拉、Sheet |
| `muted` / `muted-foreground` | 次级表面、次级文字 |
| `faint` | 三级文字（时间戳、禁用、提示） |
| `primary` / `primary-foreground` / `primary-soft` / `primary-hover` | 主操作 |
| `secondary` / `accent` | 次操作、hover 表面 |
| `destructive` / `destructive-foreground` / `destructive-soft` / `destructive-hover` | 危险操作 |
| `success` / `success-soft` | 在线、成功 |
| `warning` / `warning-soft` | 受限、超时、待处理 |
| `border` / `border-soft` / `border-strong` | 1px 分隔线、卡片内部分隔（soft）、强调边框 |
| `ring` | 焦点环 |
| `chart-1..5` | 图表 |

`success`、`warning`、`faint` 与 `*-soft` 是 shadcn 之外的**项目扩展**：
设备状态需要四种语义色，且 Chromium 81 无法计算 `color-mix()`，所以
**禁止写 `bg-x/10` 这类透明度工具类**，改用不透明的 `*-soft` 令牌。

## 3. 组件清单

基元（`src/components/ui/`）：
`alert-dialog` `alert` `badge` `breadcrumb` `button` `card` `dialog`
`dropdown-menu` `empty` `field` `input` `label` `popover` `progress`
`scroll-area` `select` `separator` `sheet` `skeleton` `spinner` `switch`
`table` `tabs` `textarea` `toast` `toggle` `tooltip`

业务组合（`src/components/`）：
`app-shell`（侧栏 / 底部 dock / 更多面板）
`page-header`（标题 + 描述 + 操作区，`MetaChip` 映射 Badge 变体）
`panel`（**Card 之上的唯一内容容器**：无外边框、`rounded-2xl`、`shadow-sm`；
头部 `px-6`，内容 `p-6`，可选 `flush` 列表模式；`PanelRow` 是固定 80px 键列）
`section`（`panel` 的兼容包装，legacy 页面仍可用）
`stat-card`（`StatStrip` / `StatCell` 指标条，`StatCard` 单卡）
`device-card`（设备卡）
`status-badge`（唯一的在线状态渲染）
`meter` / `ring-meter`（Progress 与 SVG 仪表）
`async-state`（Banner / ErrorBlock / EmptyBlock / LoadingBlock / Pager）
`confirm-dialog` `frame-view` `error-boundary` `live-bridge`

规则：

- 页面只组合这些组件；不要新增第二套按钮/卡片/表格。
- 变体不足时先加有语义的变体（如 `success`、`warning`），不要为单个页面
  增加一次性样式。
- 危险操作一律走 `ConfirmDialog`，并在异步操作完成后再关闭。
- 所有状态色只表达状态，不用于装饰。

## 4. 布局与节奏

- 桌面：240px 固定侧栏 + 主内容（最大 1360px，`px-8 xl:px-10`，底部 `pb-10`）；
  移动端：顶部栏 + 底部 dock（4 个目的地 + 更多 Sheet）；手表：同样的 dock，
  图标化、更紧凑。
- 页面级容器只负责纵向间距和栅格，**不得再设置自己的 `max-width`**；所有控制台
  页面统一继承 AppShell 的 1360px 容器，保证设备墙、系统、设置、归档、审计与
  设备详情左右边缘一致。
- 页面底色 `#f7f7f8`（暗色 `#09090b`），卡片 `#ffffff`（暗色 `#0d0d10`）：
  用**一层色阶 + 柔和阴影**区分 Z 轴。`Card`/`Panel` 无外边框、`rounded-2xl`
  （16px）、`shadow-sm`；弹层才用 `shadow-pop`。表格行、列表项、指标条内部分隔
  仍保留 1px `border` / `border-soft`，但不给卡片加外框。
- 面板：`Panel` 内部就是 shadcn `Card`；头部 `px-6 pt-6 pb-3`（无分隔线），
  内容 `p-6`。列表/表格传 `flush`，行内边距 `px-6`；行分隔线统一 `border`。
- 字号（14px 基准）：
  - H1 `text-2xl`（20–24px）`font-semibold`；页头描述 `text-sm`。
  - 卡片标题 `text-base`（16px）`font-semibold`；描述 `text-xs`。
  - 正文、表单、表格、导航项 `text-sm`（14px）。
  - 次级信息（时间戳、单位、提示）`text-xs`（12px），`muted-foreground`；
    更弱一级用 `faint`。
  - 指标数字 `text-2xl`（24px）`font-semibold tracking-tight`，大屏可到 30px；`.tnum`。
  - KPI 卡：Label `text-sm text-muted-foreground`，数值 `text-2xl`，说明
    `text-xs text-faint`；内容顶部对齐，卡片靠栅格等高。
- 侧栏：Logo 区 `px-4 py-5`；分组标题 `px-2 py-2 text-xs font-semibold`；
  菜单项 `px-2 py-2`、图标 `size-4 stroke-[1.5]`；选中项 `bg-secondary` +
  左侧 4px 黑条；状态与版本在底部第一行，图标操作在第二行。
- Tabs（line）：左对齐撑满容器，2px 圆角下划线（`rounded-full`），
  选中态切换用 200ms opacity 过渡。
- 数字一律 `.tnum`（等宽数字）。
- 触控目标不小于 44px（`.tap` / `pointer: coarse` 规则）。

## 5. Chromium 81（手表 WebView）兼容

**兼容链路必须保留**，除非有实测证据证明可以删除。当前策略：

1. `vite-plugin-legacy-js.ts`：构建后 Babel 降到 chrome 81（私有方法、逻辑赋值
   等），不使用 esbuild 降级（会破坏 Base UI 菜单）。
2. `vite-plugin-legacy-css.ts`：为不支持 `@layer` 的引擎生成扁平 stylesheet，
   并在其中：
   - 展开 `@layer`；
   - 把 `padding-inline/block`、`margin-inline/block`、`inset-inline/block`
     等逻辑属性改为物理属性（Chromium 81 只有 `-start/end` 长写，没有简写）；
   - 为 flex `gap` 生成 margin 回退。
3. `src/compat.ts`：仅在缺少时补齐 `Object.hasOwn`、`structuredClone`、
   `Array.prototype.at`、`MediaQueryList.addEventListener`。
4. 现代 CSS 的使用约束：
   - 颜色：只用不透明令牌；不用 `color-mix()`、`oklch()`、透明度后缀。
   - 位移/旋转/缩放：不用 `translate-*` / `rotate-*` / `scale-*` 工具类
     （它们编译成独立的 `translate`/`rotate`/`scale` 属性，Chromium 104）；
     改用 `transform`（见 `.ui-dialog`、`.switch-thumb`、`.sheet-panel-*`）。
   - 选择器：不依赖 `:has()`（Chromium 105）实现功能；不依赖容器查询。
   - 尺寸：不用 `aspect-ratio` 工具类，改用 `.ratio-*`（带 padding 回退）；
     不用 `100dvh`，改用 `.app-vh`（带 `@supports` 回退）。
   - 动画：只动画 `transform`/`opacity`，`prefers-reduced-motion` 下关闭。

`scripts/check-legacy-artifacts.mjs` 会对 `dist/` 做产物级检查，构建后运行。

## 6. 自检

```bash
pnpm typecheck   # tsc -b --noEmit
pnpm build       # tsc -b && vite build（含 compat 产物）
node scripts/check-legacy-artifacts.mjs
```
