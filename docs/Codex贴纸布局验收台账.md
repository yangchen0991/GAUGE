# Codex 模式桌面贴纸 · 尺寸验收台账与布局规格

> 产出时间：2026-09-22 17:2x–17:4x（AutoClaw agent-aggd5i 会话）。修复提交：dd07da1。
> 验证工具：Playwright（Chromium 1237）本地探针 + 连续尺寸扫描；证据目录见文末索引。

## 1. 根因分析结论（含触发条件与复现路径）

| # | 根因 | 触发条件 | 复现路径 | 证据 |
|---|---|---|---|---|
| R1 | **wide 空列占位**：`body.wx-expanded .col-left{flex:0 1 48%}` 在 Codex 模式下卡片被隐藏但列容器仍占 48% 宽，右列被挤进 52% 窄区（约 355px），图表与任务卡被压扁、左侧大片空白 | 切换 Codex 模式 → 点击展开（wide 740×460） | codex-wide 截图（修复前）：右列内容压缩 | BEFORE-codex-wide.png |
| R2 | **展开网格卡片缺 flex 约束**：codex-card 作为 grid item 在 wide 规则里丢失 display:flex，任务列表 flex:1 失效后穿透卡片边界 | Codex + wide + 高度不足视口（如 1100×300） | codex-ultrawide（修复前）：越界×3（task-item bottom=382 > 300） | probe-172406/layout-report.json |
| R3 | **compact 主区滚动违约**：Codex 内容（token-grid+quota 行+任务列表）天然高于 ZCode，main overflow:auto 溢出滚动，破坏贴纸单屏契约 | Codex + compact（380×460） | 基线扫描：codex-compact 50/50 尺寸点 mainScrollable=true | sweep-172958/sweep-report.json |
| R4 | **矮视口内容最小高度超支**：token-grid/quota-line/credits 固定行高叠加超出可用空间，w≤560 且 h≤560 时逐元素穿透视口 | Codex + 任意宽度 + 矮视口（h≤560） | 扫描（修复中）：quota-line 越界×39、token-grid×18 | sweep-173101/sweep-report.json |

## 2. 布局规格（修复后，网格功能主义）

### 2.1 尺寸档位与断点

| 档位 | 触发条件 | 布局 |
|---|---|---|
| compact（默认） | 非 wx-expanded | 单列全宽：图表（72–104px）→ Codex 卡片（flex:1 内部滚动） |
| wide 窄幅 | wx-expanded 且宽 ≤560px | 回落单列：图表压缩至 64px 基线，卡片 flex:1 滚动 |
| wide 标准 | wx-expanded 且宽 >560px | **5:7 双列网格**：左=图表（跨 2 行），右=Codex 卡片（跨 2 行） |
| wide 超宽 | wx-expanded 且宽 ≥980px | **三列网格**：图表独占顶部整行（4:4:4），任务卡占下方 2/3 宽 |
| 极端宽高比 | 高 ≤340px | 图表 56–80px + 信息区折叠（见 2.3） |

### 2.2 栅格与缩放规则

- 所有 codex 网格轨道用 `minmax(0, Nfr)` 防内容撑爆（0 最小值是防溢出关键）。
- 卡片与滚动链：`main(overflow:hidden) → col-right(overflow:hidden) → card(overflow:auto) → task-list(overflow:auto)`，每级 min-height:0 打通收缩链。
- 图表高度：compact `clamp(72px,16vh,104px)`；wide `min-height:96px flex:1`；窄幅回落 `clamp(64px,16vh,104px)`；超扁 `clamp(56px,14vh,80px)`。
- 圆角与设计令牌零改动（#widget 28px、碳黑/琥珀体系不变）。

### 2.3 矮视口渐进降级（内容优先级）

1. 高 ≤460px：quota 行收起"重置时间"列（保数值）。
2. 高 ≤400px：codex-summary（今日摘要行）隐藏（主信息已在 header）。
3. 高 ≤340px：图表压至 56px 下限 + token-grid 收紧间距。

### 2.4 后续扩展方法

- 新增尺寸档位：在 §2.1 表格对应断点加 `@media` 分支，选择器前缀 `body.codex-mode[.wx-expanded]`，网格轨道改 `grid-template-columns/rows` 即可。
- 新增内容区块：放入 codex-card 内部即可继承滚动链；若放卡片外，需自带 min-height:0。
- ZCode 侧如需同样处理：本批所有规则都以 `body.codex-mode` 前缀隔离，复制分支改前缀即可，互不影响。

## 3. 尺寸验收台账（全部真实执行）

### 3.1 六态探针（修复后全部干净）

| 状态 | 视口 | 越界 | 重叠 | 主区滚动 | 截图 |
|---|---|---|---|---|---|
| zcode-compact | 380×460 | 0 | 0 | 否 | zcode-compact.png |
| zcode-wide | 740×460 | 0 | 0 | 否 | zcode-wide.png |
| codex-compact | 380×460 | 0 | 0 | 否 | codex-compact.png |
| codex-wide | 740×460 | 0 | 0 | 否 | codex-wide.png |
| codex-ultrawide | 1100×300 | 0 | 0 | 否 | codex-ultrawide.png |
| codex-tall | 300×700 | 0 | 0 | 否 | codex-tall.png |

### 3.2 连续尺寸扫描（280 点 = 10 宽 × 7 高 × 4 模式）

| 模式 | 修复前基线 | 修复后 | 结论 |
|---|---|---|---|
| codex-wide | 42 失败点（越界+滚动） | **0 失败** | 全尺寸修复 |
| codex-compact | 50 失败点（全滚动） | **0 失败** | 全尺寸修复 |
| zcode-wide | 20 滚动点 | 20 滚动点 | 与基线完全一致（既有设计，零回归） |
| zcode-compact | 40 滚动点 | 40 滚动点 | 同上 |

扫描矩阵：宽 300/380/480/560/700/740/860/980/1100/1200 × 高 260/300/360/420/460/560/700。

### 3.3 修复前后像素差异（布局实际变化量化）

| 状态 | 差异像素占比 | 变化内容 |
|---|---|---|
| codex-wide | 14.3%（48599/340400） | 空列占位消除 + 5:7 双列网格 |
| codex-compact | 6.5%（11330/174800） | 图表压缩 + 卡片滚动收敛 |

### 3.4 回归验证

| 项 | 结果 |
|---|---|
| pytest 全量 | 93 passed |
| mypy desktop | Success: no issues found in 23 source files |
| node 交互套件 | 通过 |
| ZCode 基线对照 | 60 个滚动点与修复前完全一致（git stash 基线法实测） |
| 改动范围 | 仅 desktop/widget.html（+66 行 CSS，无 JS/DOM/数据变更） |

## 4. 真机复验指引（用户侧）

桌面版贴纸直接加载 widget.html（无构建中间层），重启托盘或重开贴纸即生效：
1. 切换 Codex 模式 → 点击展开：应看到图表与任务卡左右双列（5:7），无左侧空列。
2. 拖动调整窗口大小（宽 300–1200、高 260–700 任意值）：无重叠/穿透/整页滚动；任务列表超出时在卡片内滚动。
3. ZCode 模式同样拖动：表现与修复前一致（窄/矮尺寸允许滚动为既有行为）。

## 5. 证据索引（J:\zcode\agent-monitor\output\）

- codex-layout-probe-20260922-172406/：修复前六态截图+测量（错乱证据）
- codex-layout-probe-20260922-173506/：修复后六态截图+测量（验收通过）
- codex-layout-sweep-20260922-172958/：修复前基线 280 点扫描（zcode 60 滚动点基线）
- codex-layout-sweep-20260922-173424/：修复后 280 点扫描（codex 全绿）
- 中间迭代扫描 172633/172751/173101/173235：根因定位过程证据
