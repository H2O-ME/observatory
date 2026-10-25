# Observatory WebUI · Design System 0.5（扁平）

> 本文件是 UI 的唯一视觉/工程契约。任何页面重写先读这里，再读自己的旧实现。
> 目标：**手表 / 手机 / 桌面三端都像专门为它设计的**，不是把桌面版缩小。
> 0.5 变更：**应用户要求放弃立体拟态（玻璃拟态）**，全面转向扁平现代风
> （Linear/Vercel 质感）。动效保留且克制，仍以 Aceternity 为动效库。

## 1. 视觉方向

**Flat mission control（扁平任务控制台）**：实色底 + 实色面板 + 1px 细线边框。
层级靠**颜色深浅阶梯**（bg < panel < panel-strong < raised）和**边框强度**表达，
**不使用**毛玻璃（backdrop-filter）、半透明面板、大面积投影、环境光晕。

- 品牌信号色：`brand`（蓝）→ `brand-2`（紫）→ `brand-3`（青）；渐变只允许出现在
  文字（`.text-gradient`）、流动边框（MovingBorder/HoverBorderGradient）和主 CTA
  （ShimmerButton）上，不允许出现在整块面板背景。
- 状态色：`ok` / `warn` / `danger` 只表达状态，不做装饰。
- 数据用等宽字体 + tabular-nums；标签用 letter-spacing 的小号大写。
- 阴影只有一个：`--sh-pop`，给真正悬浮的层（Dialog/Popover/Tooltip/Toast/Dock）。
  `--sh-card`、`--sh-raised` 恒为 `none`，卡片靠边框。
- 动效只做"进入、悬停、环境细节"，不做持续抢注意力的动画；
  `prefers-reduced-motion` 全关。
- 全局背景：纯色 `bg` + 极淡工程网格（`GridPattern` + `--c-grid-line`）。
  登录页额外允许 `BackgroundBeams`（细线光束）。**禁止** Aurora/Spotlight/
  Lamp/Meteors 这类氛围层（相关组件已删除）。

## 2. 三端断点

`useViewport()`（`src/hooks/use-viewport.ts`）：

| 端 | 判定 | 导航 | 内容 |
| --- | --- | --- | --- |
| watch | `w <= 480 && h <= 560`，或 `w <= 340` | 顶部 40px 品牌条 + 底部 **图标 Dock**（5 项 + 更多） | 单列，`px-2.5`，标题 17-18px，正文 12-13px，触控 ≥44px |
| mobile | `w <= 900` | 顶部 56px 品牌条 + 底部 **FloatingDock**（5 项 + 更多） | 单列/少量双列，`px-4`，标题 24px，正文 13.5-14px |
| desktop | 其余 | 左侧 252px 浮动侧栏 | 12 列 bento，`px-6/8`，标题 30-32px，正文 14px |

## 3. 令牌（index.css）

只使用语义 token，不要写死 hex（渐变装饰除外）：

`bg`、`bg-2`、`panel`、`panel-strong`、`raised`、`sunken`、`sunken-strong`、
`line`、`line-strong`、`ink`、`soft`、`faint`、`brand`、`brand-2`、`brand-3`、
`brand-ink`、`brand-soft`、`ok`、`warn`、`danger`、`ok-soft`、`warn-soft`、
`danger-soft`、`grid-line`。

面板全部**不透明实色**（light: `#fff`；dark: `#10131b` 系）。Tailwind 映射：
`bg-panel`、`text-ink`、`text-soft`、`text-faint`、`border-line`、`bg-brand`、
`bg-danger-soft` 等。**alpha 简写（`bg-brand/10`）在 Chromium 81 会整条丢弃**
（Tailwind v4 把它放在 `@supports color-mix` 里）——需要 alpha 面时用已定义的
`*-soft` token 或 `style={{ backgroundColor: "rgba(...)" }}`。

## 4. 共享类与组件

### CSS 工具类（index.css）

- `.surface`：标准面板（实色 panel + 1px line + 圆角，**无阴影无模糊**）
- `.surface-strong`：强调面板（panel-strong + line-strong）
- `.surface-quiet`、`.surface-danger`、`.surface-warn`、`.surface-ok`
- `.surface-hover`：悬停仅边框变亮 + 面板加深一档（无位移、无阴影）
- `.glass-chrome`：导航条（现在是**实色 bg + 细线**，名字保留是为了少改调用点）
- `.eyebrow`：11px 大写、字距 0.18em、faint 色
- `.display-title`：页面主标题（clamp 字号）
- `.text-gradient`：品牌渐变文字（`background-clip:text`）
- `.tnum`：等宽 + 表格数字
- `.tap`：min-height 44px（粗指针）
- `.hide-scrollbar`、`.scroll-x`
- `.safe-bottom` / `.safe-top` / `.pb-safe` / `.pt-safe`（mobile.css）
- `.ui-dialog`：所有 Dialog 内容复用；桌面居中 + `--sh-pop`，手机&手表底部 Sheet
- `.ratio-16-10`、`.ratio-16-9`：aspect-ratio + Chromium 81 padding-top 回退
- `.inset-full`：`top:0;right:0;bottom:0;left:0`（代替 `inset-0`）

### 共享 React 原语

| 文件 | 导出 | 用途 |
| --- | --- | --- |
| `components/page-header.tsx` | `PageHeader` | eyebrow + 标题 + 描述 + 右侧动作 + 可选返回 |
| `components/section.tsx` | `Section`, `SectionHeader` | 带图标/提示/动作的面板分区 |
| `components/stat-card.tsx` | `StatCard`, `StatGrid` | 统计磁贴（icon + value + label + hint） |
| `components/status-pill.tsx` | `StatusBadge` | 设备在线态 |
| `components/async-state.tsx` | `Banner`, `ErrorBlock`, `EmptyBlock`, `LoadingBlock`, `Pager` | 统一加载/空/错误 |
| `components/meter.tsx` | `Meter`, `RingMeter` | 线性/环形指标（旋转用内联 `transform`） |
| `components/aceternity/*` | 见 §5 | 背景与动效 |
| `components/ui/*` | Button/Input/Select/Switch/Dialog… | 基础控件（Base UI） |

## 5. Aceternity 组件清单（全部 Chromium 81 安全实现）

| 组件 | 路径 | 用在哪 |
| --- | --- | --- |
| `BackgroundBeams` | `aceternity/background-beams.tsx` | 登录页、设备 hero |
| `GridPattern` | `aceternity/grid-pattern.tsx` | 全局背景、空态、hero |
| `Sparkles` | `aceternity/sparkles.tsx` | 登录、空态点缀 |
| `MovingBorder` | `aceternity/moving-border.tsx` | 侧栏激活项 |
| `HoverBorderGradient` | `aceternity/hover-border-gradient.tsx` | 登录提交、主行动 |
| `TextGenerateEffect` | `aceternity/text-generate-effect.tsx` | 登录标题 |
| `BentoGrid`/`BentoGridItem` | `aceternity/bento-grid.tsx` | 文件页等网格 |
| `GlowCard` | `aceternity/glow-card.tsx` | 重点卡片（指针跟随光斑 + 渐变描边，无投影） |
| `Timeline` | `aceternity/timeline.tsx` | 命令记录、审计 |
| `TracingBeam` | `aceternity/tracing-beam.tsx` | 时间线分组 |
| `FloatingDock` | `aceternity/floating-dock.tsx` | 手机/手表导航 |
| `AnimatedTooltip` | `aceternity/animated-tooltip.tsx` | 桌面侧栏图标按钮 |
| `ShimmerButton` | `aceternity/shimmer-button.tsx` | 保存、上传等主按钮（扫光保留，光泽投影已去） |

已删除（0.5 扁平化裁撤，勿再引入）：`AuroraBackground`、`LampEffect`、
`Spotlight`、`Meteors`、`InfiniteMovingCards`。

要求：每个页面至少使用 2 种 Aceternity 组件（背景层可继承自 shell，页面内至少
1 个显式组件）。

## 6. Chromium 81 硬约束（手表 WebView）

**禁止在关键布局使用：**

1. `inset-0` / `inset-x-*` / `inset-y-*`（CSS `inset` 简写 Ch87）→ 用 `.inset-full` 或 longhand。
2. Tailwind `translate-*` / `rotate-*` / `scale-*`（Ch104 独立 transform 属性）→
   `style={{ transform: ... }}` 或自定义 class。
3. `aspect-video` 之外的 `aspect-*`（Ch88）→ `.ratio-*` / 固定高度。
4. `100dvh`（Ch108）→ `.app-vh` / `.app-vh-min`（已有回退）。
5. `color-mix()` / `oklch()` 裸写（Ch111）→ 令牌或 rgba。
6. `@property` 动画依赖、`@container`、`:has()`、`overflow: clip`、`text-wrap: balance`。
7. 关键 flex 行使用 `gap`（flex gap Ch84）：用 **grid gap**、`space-x-*` 或 `mr/ml`。
8. `.map` 里给列表项加 `key`，不要用数组 index。

**布局纪律：**

- 每个 flex/grid 子项若无固定宽度，必须 `min-w-0`；文本容器 `min-w-0` + `truncate`/`break-words`。
- 页面根节点 `w-full max-w-full overflow-x-hidden`；局部横向滚动必须
  `overflow-x-auto hide-scrollbar` 且不撑宽父级。
- 不允许固定 `width` 大于 100%；表格类数据在 mobile/watch 改为卡片/键值行。
- 图标按钮必须有 `aria-label` + `title`；触控目标 ≥44px（`.tap`）。
- 所有页面必须处理 loading / error / empty 三态。

## 7. 页面模式

1. **页面骨架**：`<PageHeader ... />` + 可选 hero（`GlowCard`）+ 内容网格。
2. **hero 统计**：桌面 4 列、手机 2 列、手表 2 列（数字大、标签小）。
3. **列表**：桌面表格感行（grid 列）；手机卡片行；手表大行（只留主信息 + 一个主操作）。
4. **表单**：桌面 label 左、控件右；手机/手表上下堆叠；保存动作在 PageHeader 或
   sticky bar（手机要避开 Dock）。
5. **危险操作**：独立 `surface-danger` 卡，确认对话框（`.ui-dialog` 自动变 Sheet）。
6. **详情弹层**：图片/JSON 用 Dialog + `.ui-dialog`。
7. **空态**：`EmptyBlock`（内含 GridPattern/Sparkles）+ 明确下一步 CTA。
8. **实时画面**：16:9 容器（`.ratio-16-9`），工具栏在画面下方，状态数字用 `.tnum`。

## 8. 本地预览与验收

### Mock 服务器（无真服务器也能全链路预览）

```bash
cd webui && npm run build && node scripts/mock-server.mjs 8974
# 打开 http://127.0.0.1:8974 —— 任意密码可登录，6 台假设备覆盖全状态
```

### 验收清单

- `npm run build` 通过（tsc + vite）。
- 1600×900、390×844、320×320 三视口截图：
  `/wall`、`/device/:uid/{overview,live,timeline,files,commands,settings}`、
  `/backup`、`/updates`、`/audit`、`/system`、`/settings`、`/account`、登录页。
- 每个页面 `document.documentElement.scrollWidth <= clientWidth`（无横向滚动）。
- 手表视口所有交互目标 ≥44px；无内容被底部 Dock 遮挡（内容区 `padding-bottom`）。
- 刷新/轮询/命令/文件/实时控制逻辑与接口调用不变；只改展示层。
