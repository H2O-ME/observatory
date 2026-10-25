# Observatory WebUI · Design System 0.4

> 本文件是 0.4 UI 重写的唯一视觉/工程契约。任何页面重写先读这里，再读自己的旧实现。
> 目标：**手表 / 手机 / 桌面三端都像专门为它设计的**，不是把桌面版缩小。

## 1. 视觉方向

**Deep-field mission control（深空任务控制台）**：近黑/晨雾底色 + 极光与光束背景 +
半透明仪器面板 + 青蓝→靛紫渐变信号色。信息密度高，但层级必须一眼可读：
页面大标题 → 分区标题 → 数据。禁止再把所有字压到 11px。

- 品牌信号色：`brand`（蓝）→ `brand-2`（紫）→ `brand-3`（青）。
- 状态色：`ok` / `warn` / `danger` 只表达状态，不做装饰。
- 数据用等宽字体 + tabular-nums；标签用 letter-spacing 的小号大写。
- 动效只做“进入、悬停、环境光”，不做持续抢注意力的动画；`prefers-reduced-motion` 全关。

## 2. 三端断点

`useViewport()` 已存在（`src/hooks/use-viewport.ts`），重写为：

| 端 | 判定 | 导航 | 内容 |
| --- | --- | --- | --- |
| watch | `w <= 480 && h <= 560`，或 `w <= 340` | 顶部 40px 品牌条 + 底部 **图标 Dock**（5 项 + 更多） | 单列，`px-2.5`，标题 17-18px，正文 12-13px，触控 ≥44px |
| mobile | `w <= 900` | 顶部 56px 品牌条 + 底部 **FloatingDock**（5 项 + 更多） | 单列/少量双列，`px-4`，标题 24px，正文 13.5-14px |
| desktop | 其余 | 左侧 264px 浮动侧栏 | 12 列 bento，`px-6/8`，标题 30-32px，正文 14px |

> 手机横屏/平板（≤900）仍走 mobile；手表方形屏（454×454 等）必须命中 watch。

## 3. 令牌（index.css 已定义）

只使用语义 token，不要写死 hex（渐变装饰除外）：

`bg`、`panel`、`panel-strong`、`raised`、`sunken`、`line`、`line-strong`、
`ink`、`soft`、`faint`、`brand`、`brand-2`、`brand-3`、`brand-ink`、`brand-soft`、
`ok`、`warn`、`danger`、`ok-soft`、`warn-soft`、`danger-soft`。

Tailwind 映射（已有）：`bg-panel`、`text-ink`、`text-soft`、`text-faint`、`border-line`、
`bg-brand`、`text-brand`、`bg-danger-soft` 等。**alpha 简写（`bg-brand/10`）在 Chromium 81
会整条丢弃**（Tailwind v4 把它放在 `@supports color-mix` 里），手表上会变成透明——
需要 alpha 面时用已定义的 `*-soft` token 或 `style={{ backgroundColor: "rgba(...)" }}`。

## 4. 共享类与组件

### CSS 工具类（index.css）

- `.surface`：标准玻璃面板（panel + 1px line + 18px 圆角 + 阴影）
- `.surface-strong`、`.surface-quiet`、`.surface-danger`
- `.surface-hover`：悬停抬升 + 描边变亮（使用 `transform`，不用 Tailwind translate）
- `.glass-chrome`：导航栏玻璃（手表上自动关闭 backdrop-filter）
- `.eyebrow`：11px 大写、字距 0.18em、soft 色
- `.display-title`：页面主标题（clamp 字号）
- `.text-gradient`：品牌渐变文字（`background-clip:text`）
- `.tnum`：等宽 + 表格数字
- `.tap`：min-height 44px（粗指针）
- `.hide-scrollbar`
- `.lift`：hover `transform: translateY(-2px)`
- `.ui-dialog`：所有 Dialog 内容复用；自动桌面居中 / 手机&手表底部 Sheet
- `.ratio-16-10`、`.ratio-16-9`：aspect-ratio + Chromium 81 padding-top 回退
- `.inset-full`：`top:0;right:0;bottom:0;left:0`（代替 `inset-0`）

### 共享 React 原语

| 文件 | 导出 | 用途 |
| --- | --- | --- |
| `components/page-header.tsx` | `PageHeader` | eyebrow + 标题 + 描述 + 右侧动作 + 可选返回 |
| `components/section.tsx` | `Section`, `SectionHeader` | 带图标/提示/动作的面板分区 |
| `components/stat-card.tsx` | `StatCard`, `StatGrid` | 统计磁贴（icon + value + label + hint） |
| `components/status-pill.tsx` | `StatusBadge`（保留旧 API） | 设备在线态 |
| `components/async-state.tsx` | `Banner`, `ErrorBlock`, `EmptyBlock`, `LoadingBlock`, `Pager` | 统一加载/空/错误；重写视觉但保留 props |
| `components/meter.tsx` | `Meter`, `RingMeter` | 线性/环形指标 |
| `components/aceternity/*` | 见 §5 | 背景与装饰 |
| `components/ui/*` | Button/Input/Select/Switch/Dialog/Tabs… | 基础控件（Base UI） |

## 5. Aceternity 组件清单（全部 Chromium 81 安全实现）

| 组件 | 路径 | 用在哪 |
| --- | --- | --- |
| `AuroraBackground` | `aceternity/aurora-background.tsx` | AppShell 全局背景 |
| `BackgroundBeams` | `aceternity/background-beams.tsx` | 登录页、设备墙 hero |
| `LampEffect` | `aceternity/lamp-effect.tsx` | 登录页顶部灯罩光 |
| `Spotlight` | `aceternity/spotlight.tsx` | 登录、hero、实时画面 |
| `Meteors` | `aceternity/meteors.tsx` | 登录、空态 |
| `GridPattern` | `aceternity/grid-pattern.tsx` | 登录、空态、hero |
| `MovingBorder` | `aceternity/moving-border.tsx` | 侧栏激活项、主按钮、Tabs 激活 |
| `HoverBorderGradient` | `aceternity/hover-border-gradient.tsx` | 登录提交、上传、主行动 |
| `TextGenerateEffect` | `aceternity/text-generate-effect.tsx` | 登录标题、空态大标题 |
| `BentoGrid`/`BentoGridItem` | `aceternity/bento-grid.tsx` | 设备墙统计、系统、概览 |
| `GlowCard` | `aceternity/glow-card.tsx` | 所有重点卡片（指针跟随光斑 + 渐变边） |
| `Timeline` | `aceternity/timeline.tsx` | 命令记录、审计 |
| `TracingBeam` | `aceternity/tracing-beam.tsx` | 时间线按小时分组 |
| `FloatingDock` | `aceternity/floating-dock.tsx` | 手机/手表导航 |
| `InfiniteMovingCards` | `aceternity/infinite-moving-cards.tsx` | 审计顶部最近操作跑马灯 |
| `AnimatedTooltip` | `aceternity/animated-tooltip.tsx` | 桌面侧栏/图标按钮 |
| `ShimmerButton` | `aceternity/shimmer-button.tsx` | 保存、上传等主按钮 |
| `Sparkles` | `aceternity/sparkles.tsx` | 登录/空态点缀 |

要求：每个页面至少使用 2 种 Aceternity 组件（背景层可继承自 shell，页面内至少 1 个显式组件）。

## 6. Chromium 81 硬约束（手表 WebView）

**禁止在关键布局使用：**

1. `inset-0` / `inset-x-*` / `inset-y-*`（CSS `inset` 简写 Ch87）→ 用 `.inset-full` 或 `top-0 right-0 bottom-0 left-0`。
2. Tailwind `translate-*` / `rotate-*` / `scale-*`（Ch104 的独立 transform 属性）→ 用 `style`/自定义 class 的 `transform`。
3. `aspect-video` 之外的 `aspect-*`（Ch88）→ 用 `.ratio-16-10` / `.ratio-16-9` / 固定高度；不要把内容塞进无回退的比例盒。
4. `100dvh`（Ch108）→ `.app-vh` / `.app-vh-min`（已有回退）。
5. `color-mix()` / `oklch()` 裸写（Ch111）→ 令牌或 rgba。
6. `@property` 动画依赖、`@container`、`:has()`、`overflow: clip`、`text-wrap: balance`。
7. 关键 flex 行使用 `gap`（flex gap Ch84）：手表上间距会消失。用 **grid gap**（Ch57+）、`space-x-*`（margin）或 `mr/ml`。
8. `backdrop-filter` 无 `-webkit-` 前缀（.glass-* 已处理；手表媒体查询里已关闭）。
9. `.map` 里给列表项加 `key`，不要用数组 index（会闪烁）。

**布局纪律：**

- 每个 flex/grid 子项若无固定宽度，必须 `min-w-0`；文本容器 `min-w-0` + `truncate`/`break-words`。
- 页面根节点 `w-full max-w-full overflow-x-hidden`，任何局部横向滚动必须 `overflow-x-auto hide-scrollbar` 且不撑宽父级。
- 不允许固定 `width` 大于 100%；表格类数据在 mobile/watch 改为卡片/键值行。
- 图标按钮必须有 `aria-label` + `title`；触控目标 ≥44px（`.tap`）。
- 所有页面必须处理 loading / error / empty 三态。

## 7. 页面模式

1. **页面骨架**：`<PageHeader ... />` + 可选 hero（`GlowCard`/`BackgroundBeams`）+ 内容网格。
2. **hero 统计**：桌面 4 列、手机 2 列、手表 2 列（数字大、标签小）。
3. **列表**：桌面表格感行（grid 列）；手机卡片行（名称/元信息/操作分离）；手表大行（只留主信息 + 一个主操作）。
4. **表单**：桌面 label 左、控件右；手机/手表上下堆叠；保存动作固定在顶部 PageHeader 或底部 sticky bar（手机要避开 Dock）。
5. **危险操作**：独立 `surface-danger` 卡，确认对话框（`.ui-dialog` 自动变 Sheet）。
6. **详情弹层**：图片/JSON 用 Dialog + `.ui-dialog`；手机横向宽度 `calc(100% - 1rem)`，手表全屏 sheet。
7. **空态**：`EmptyBlock`（内含 GridPattern/Sparkles）+ 明确下一步 CTA。
8. **实时画面**：16:9 容器（`.ratio-16-9`），工具栏在画面下方，状态数字用 `.tnum`。

## 8. 验收清单

- `npm run build` 通过（tsc + vite）。
- 1600×900、390×844、320×320 三视口全部路由截图：
  `/wall`、`/device/:uid/{overview,live,timeline,files,commands,settings}`、
  `/backup`、`/updates`、`/audit`、`/system`、`/settings`、`/account`、登录页。
- 每个页面 `document.documentElement.scrollWidth <= innerWidth`（无横向滚动）。
- 手表视口所有交互目标 ≥44px；无内容被底部 Dock 遮挡（内容区 `padding-bottom`）。
- 刷新/轮询/命令/文件/实时控制逻辑与接口调用不变；只改展示层。
