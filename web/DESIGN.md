---
version: 1.0
name: Keepsake 控制台设计规范
description: 这是 Keepsake 控制台（web/）的唯一权威设计规范 —— 改动任何 UI 前必读，数值与 tokens.css 冲突时一律以 tokens.css 为准。
适用范围: web/ 下所有页面与组件（列表、看板、检索、常驻层、记忆维护、弹框、抽屉、登录/初始化页）
唯一配置源: web/src/styles/tokens.css
可视化目录: web/preview.html（浅色） / web/preview-dark.html（暗色）
术语: 常驻层 / 记忆条目 / 记忆维护 / 知识库 / 看板
最后校准: 2026-10-14（记忆维护独立页 + 侧栏五入口）
---

# Keepsake 控制台 · 设计规范

> **读法**：改 UI 前先读完本文件，再看 `web/preview.html` 对一眼实物，最后只写样式。
> **冲突处理**：本文件是 `web/src/styles/tokens.css` 的**文档**，不是它的替代品。
> 两者若有出入，**一律以 tokens.css 为准**，并把冲突点写进改动报告。

---

## 设计原则

1. **白 + 浅灰双底**：主区纯白（`--bg`），侧栏与筛选面板浅灰（`--panel`）。层级靠底色差 + 1px 描边表达，不靠阴影堆。
2. **单一蓝强调**：全站只有**一个彩色** —— `--accent`。第二个颜色是 `--ok` 绿点，且只用于「活着」的指示（`.dot-ok`）。成功/危险色仅在语义上不可替代处出现（危险确认、报错文字、状态色）。
3. **靠边框分层，不靠阴影**：卡片/面板/弹层都是 `1px solid var(--border)` + `var(--shadow-card)`（`0 1px 2px rgba(0,0,0,.04)`）这一档。hover 才升到 `--shadow-hover`，弹层才用 `--shadow-pop`。**禁止渐变、玻璃拟态、发光。**
4. **数据密度优先**：卡片行高紧凑、摘要截断在 3 行、控件统一 40px，一屏要能放下可比较的量。
5. **双主题同构**：浅色与暗色是**同一套结构换令牌**（`:root` / `:root[data-theme="dark"]`），不是两套样式。任何新增令牌必须两套都写、键名完全一致。

---

## 颜色令牌

以下两表是 `web/src/styles/tokens.css` 的**原样摘录**，含每一项的用途约定。
**改色只改 tokens.css**，本表与组件都只是引用方。

### 浅色（`:root`，默认）

| 令牌 | 值 | 用途 |
|---|---|---|
| `--bg` | `#ffffff` | 页面主底（`.main`、body） |
| `--bg-elev` | `#ffffff` | 抬起层底色：卡片、面板、弹层、输入框、chip 描边底 |
| `--panel` | `#f5f5f6` | 浅灰面板底：侧栏 `.nav`、筛选面板 `.filter-panel`、预格式块 `.pre` |
| `--panel-hover` | `#ececee` | 浅灰面板上的 hover（侧栏导航行 hover、分段控件轨道） |
| `--chip` | `#f1f1f2` | chip 底、行内代码底、进度条轨道 |
| `--border` | `#ececee` | 描边第 1 档：卡片 / 面板 / 弹层 / `.kv` / `.banner` |
| `--border-2` | `#e8e8ea` | 描边第 2 档：输入框、按钮、chip（`.tag`）、复制按钮 |
| `--line-3` | `#f0f0f0` | 描边第 3 档：卡内分隔线（`.card-foot` 上边线） |
| `--text` | `#1a1a1a` | 主文字：标题、正文强调、`.kv` 的值 |
| `--text-dim` | `#555555` | 次文字：正文、摘要 `.card-body`、弹框正文 |
| `--text-faint` | `#666d78` | 弱文字但**要读**：时间 `.card-time`、标签 `.stat-label`、提示 `.stat-hint` |
| `--text-ghost` | `#9a9a9e` | **装饰专用**：placeholder、分隔符、图标、禁用描边 —— 永远不承载可读信息 |
| `--accent` | `#2f6bff` | 唯一强调色：链接、选中胶囊、主按钮、logo、焦点描边 |
| `--accent-soft` | `#eaf1ff` | 强调浅底：`.chip--accent`、`.pager .pg.on`、`.soft-btn` |
| `--on-accent` | `#ffffff` | 强调色上的文字（选中侧栏项、实心主按钮） |
| `--ok` | `#22c55e` | 活着的指示：`.dot-ok`、`.stat-bar i.ok` |
| `--danger` | `#dc2626` | 危险色：确认弹框红竖条、危险按钮字、报错条 |
| `--danger-soft` | `#fdeeee` | 危险浅底：`.act-btn--danger`、`.chip--danger`、`.banner--danger` |
| `--shadow-card` | `0 1px 2px rgba(0, 0, 0, 0.04)` | 卡片/面板/分页条基础阴影 |
| `--shadow-hover` | `0 4px 12px rgba(0, 0, 0, 0.06)` | 卡片 hover 升一档 |
| `--shadow-pop` | `0 12px 32px rgba(16, 24, 40, 0.12)` | 弹框/抽屉/toast/下拉 |
| `--shadow-nav-on` | `0 2px 6px rgba(47, 107, 255, 0.25)` | 侧栏选中胶囊的蓝色投影 |
| `--mask` | `rgba(16, 24, 40, 0.45)` | 遮罩 |
| `--skeleton-from` | `#f0f0f2` | 骨架屏起色 |
| `--skeleton-to` | `#f7f7f8` | 骨架屏止色（shimmer 渐变端） |
| `--fill-dark` | `#ececee` | element-plus 深填充（表格/弹层次级区） |
| `--danger-hi` | `#ef4444` | 危险按钮 hover 加深 |
| `--accent-3` | `#c1d3ff` | element-plus primary light-3（hover 态衍生） |
| `--accent-5` | `#97b5ff` | element-plus primary light-5 |
| `--accent-7` | `#6d97ff` | element-plus primary light-7 / light-8 |
| `--accent-9` | `#eaf1ff` | element-plus primary light-9（选中行底） |
| `--accent-d2` | `#2656cc` | element-plus primary dark-2 = 主按钮 hover |

非颜色令牌（同在 `:root`）：`--r-panel/-card/-inset/-ctl/-sm/-chip` 见圆角阶梯；
`--font`（Inter 兜底系统栈）、`--mono`（ui-monospace 栈）、`--nav-w: 232px`。

### 暗色（`:root[data-theme="dark"]`）

同一组键，**只换值**。底更深、描边转半透白、主蓝提亮。

| 令牌 | 暗色值 | 与浅色的差别 |
|---|---|---|
| `--bg` | `#0b0d10` | 深黑蓝底 |
| `--bg-elev` | `#12151a` | 抬起层比主底亮一档（浅色下两者同为白） |
| `--panel` | `#12151a` | 面板底 = 抬起层 |
| `--panel-hover` | `#1b1f27` | hover 更亮 |
| `--chip` | `#1b1f27` | chip 底 |
| `--border` | `rgba(255, 255, 255, 0.08)` | 描边转半透白 |
| `--border-2` | `rgba(255, 255, 255, 0.11)` | 描边第 2 档 |
| `--line-3` | `rgba(255, 255, 255, 0.05)` | 内部分隔线 |
| `--text` | `#eef1f6` | 文字提亮 |
| `--text-dim` | `#a7b0be` | 次文字 |
| `--text-faint` | `#7c8697` | 弱但要读的文字 |
| `--text-ghost` | `#6b7280` | 装饰专用 |
| `--accent` | `#5b8cff` | 主蓝提亮（暗底上 6.15:1） |
| `--accent-soft` | `#16233d` | 强调浅底改为深蓝 |
| `--on-accent` | `#ffffff` | 不变 |
| `--ok` | `#22c55e` | 不变（暗底 8.54:1） |
| `--danger` | `#f05252` | 红提亮（暗底 5.59:1） |
| `--danger-soft` | `#2a1618` | 危险浅底改深红 |
| `--shadow-card` | `0 1px 2px rgba(0, 0, 0, 0.4)` | 暗底需要更实 |
| `--shadow-hover` | `0 4px 12px rgba(0, 0, 0, 0.5)` | |
| `--shadow-pop` | `0 16px 40px rgba(0, 0, 0, 0.55)` | |
| `--shadow-nav-on` | `0 2px 6px rgba(91, 140, 255, 0.3)` | |
| `--mask` | `rgba(11, 13, 16, 0.72)` | |
| `--skeleton-from` | `#1a1f27` | |
| `--skeleton-to` | `#232932` | |
| `--fill-dark` | `#1b2029` | |
| `--danger-hi` | `#f47070` | |
| `--accent-3` | `#4062b3` | 换肤后次序反转（暗色下 -3 比 -9 亮） |
| `--accent-5` | `#2e4680` | |
| `--accent-7` | `#1b2a4d` | |
| `--accent-9` | `#121c33` | |
| `--accent-d2` | `#7ba3ff` | 暗色下 dark-2 是**提亮**，hover 往亮走 |

---

## 对比度硬指标

**硬指标：`--text` / `--text-dim` / `--text-faint` 三档在各自主题的最差底色上必须 ≥ 4.5:1。**
最差底色 = 浅色的 `--panel #f5f5f6`、暗色的 `--panel #12151a`（面板比白底/深底更难读）。
`tests/theme.test.mjs` 与 `tests/ui-style.test.mjs` 会把这条当断言跑。

### 实测比值（WCAG 2.x 相对亮度公式，sRGB）

| 文字令牌 | 浅色 / 白底 | 浅色 / 浅灰面板 | 达标 | 暗色 / 深底 | 暗色 / 面板 | 达标 |
|---|---|---|---|---|---|---|
| `--text` | **17.40:1** | 15.97:1 | ✅ | **17.19:1** | 16.16:1 | ✅ |
| `--text-dim` | **7.46:1** | 6.84:1 | ✅ | **8.89:1** | 8.36:1 | ✅ |
| `--text-faint` | **5.22:1** | **4.79:1** | ✅ | **5.29:1** | **4.98:1** | ✅ |
| `--text-ghost` | 2.80:1 | 2.57:1 | ❌ 不要求 | 4.02:1 | 3.78:1 | ❌ 不要求 |

`--text-ghost` 落在 4.5 以下是**有意为之**：它只服务装饰与占位符（element-plus placeholder、
分隔点、图标、禁用态描边）。它一旦开始承载可读信息（时间、标签、提示），立刻改用 `--text-faint`。

### 非文字配色的实测值（记录用，非文字不适用 4.5）

| 组合 | 浅色 | 暗色 | 判定 |
|---|---|---|---|
| `--accent` 字 / 白底 | 4.50:1 | — | 恰好达标（链接） |
| `--on-accent` 白字 / `--accent` 底（侧栏选中项、实心主按钮） | 4.50:1 | **3.16:1** | ⚠️ 暗色下偏低，见下 |
| `--accent` 字 / `--accent-soft` 底（`.chip--accent`、`.pager .pg.on`） | 3.97:1 | 4.95:1 | ⚠️ 浅色下偏低 |
| `--danger` 字 / `--danger-soft` 底（`.act-btn--danger`） | 4.29:1 | 4.92:1 | ⚠️ 浅色下偏低 |
| `--text-dim` / `--panel` | 6.84:1 | 8.36:1 | ✅ |
| `--text-faint` / `--chip` | 4.62:1 | — | ✅ |

> ⚠️ 三条已知的 4.5 以下配对是**现存实现**（暗色选中项 3.16、浅色 accent-soft chip 3.97、
> 浅色 danger-soft 按钮 4.29），本轮不改动、不隐瞒。它们都用在**同时有形状/底色/位置冗余**的
> 元素上（选中项靠蓝色胶囊本身、chip 靠描边、按钮靠文案动词），但若要提分，正解是调
> tokens.css 的暗色 `--accent` / 浅色 `--accent-d2`，**不是**在组件里加 `font-weight` 硬扛。

---

## 圆角阶梯

六级，只用这六级，**禁止在组件里写裸 px 圆角**。

| 档 | 令牌 | 值 | 用在哪 |
|---|---|---|---|
| 1 | `--r-panel` | `20px` | 最大的独立容器：侧栏 `.nav`、抽屉 `.el-drawer` |
| 2 | `--r-card` | `16px` | 卡片 `.card`、统计卡 `.stat`、面板 `.panel`、筛选面板 `.filter-panel`、分页条 `.pager`、弹框 `.el-dialog` / `.el-message-box` |
| 3 | `--r-inset` | `12px` | 卡内内嵌块：键值表 `.kv`、预格式 `.pre`、提示条 `.banner`、侧栏品牌卡 `.brand-card`、logo 方块 |
| 4 | `--r-ctl` | `10px` | 控件外壳：侧栏导航行 `.item`、输入框/下拉/文本域、主题分段控件 `.seg`、toast、下拉浮层 |
| 5 | `--r-sm` | `8px` | 按钮：`.el-button`、`.ghost-btn`、`.soft-btn`、`.act-btn`、分段控件内的按钮 |
| 6 | `--r-chip` | `6px` | 小粒：`.tag`、`.chip`、分页页码 `.pg`、复制按钮 `.copy-btn`、行内 `code`、滚动条滑块 |

规律：**内层圆角 < 外层圆角**。20 → 16 → 12 → 10 → 8 → 6 从外往里递减。
`--r-inset` 只出现在卡片/面板**里面**，不与 `--r-card` 同层竞争。
`tests/ui-style.test.mjs` 断言 20/16/12/10/6 五档必须还在 tokens.css 里。

---

## 字号阶梯

**五档 + 一个展示档，最小 11px。** 不许出现档外字号。

| 档 | 值 | 用在哪 |
|---|---|---|
| 微型 | `11px` | `.stat-hint` 统计卡脚注（全局下限，再小一律升到 12px） |
| 小 | `12px` | 时间 `.card-time`、`.tag` / `.chip`、`.status-note`、`.kv` 行、`.mono`、侧栏分组标题、品牌卡、`.tip`、`.empty` |
| 次 | `13px` | 面包屑、次要说明 `.page-sub`、筛选区、`.ghost-btn` / `.soft-btn` / `.act-btn` / `.card-link`、`.stat-label`、分页条 |
| 正文 | `14px` | **基准字号**（`body` 与 element-plus `--el-font-size-base`）：正文、`.card-body`、按钮 `.el-button`、输入文字、`.page-title` 外的说明 |
| 标题 | `16px` | 卡片标题 `.card-title`、弹框标题、抽屉标题、区块标题 |
| 展示 | `22 / 27 / 32px` | `.page-title` 27px（≤900px 收 22px）、`.stat-value` 32px/700 + `letter-spacing:-0.8px` |

配套字重：数字/统计 `700`；卡片标题、区块标题、弹框标题 `600`；按钮、导航项、正文强调 `500`；其余 `400`。

---

## 间距与 8px 栅格

以 8px 为基线（4px 半档仅用于紧贴场景）。

| 场景 | 值 | 位置 |
|---|---|---|
| 卡内留白 | `20px` | `.card` padding、`.stat` padding、`.filter-panel` padding |
| 区块留白 | `24px` | `.main` padding-top、`.section-title` margin-top |
| 卡间距 | `20px` | `.card-grid` gap、`.stat-grid` gap |
| 元素间距 | `8 / 12 / 16px` | 同排元素 8px、卡内区块 12px、窄屏降级 16px |
| 分页条留白 | `12px 16px` | `.pager` padding |

各处的固定值：`.main` padding `24px 24px 40px`（底部多留，因为列表常在末尾分页）；
`.card` 内部 gap `12px`、`.card-top` gap `12px`；`.el-drawer__body` padding `20px`。

**已存在的档外值**（照抄现状，新增时不要再扩散）：`.card-tags` gap `6px`、`.filter-ctl` gap `10px`、
控件高度 `28px`（`.act-btn` / `.pager .pg` / `.seg button` 30px）、`ConfirmDialog` 正文 `12.5px`。
它们的存在理由都是「小控件塞进 8px 栅格会显得散」；改动前先想清楚是收敛还是保留。

---

## 布局规则

### 侧栏 = 独立圆角面板（不贴边）

- `position: fixed`，`top/bottom/left: 12px` —— **四周留 12px 白边，不通栏**，圆角 20px 才立得住。
- 宽 `--nav-w: 232px`（注意：不是 220px）。
- 底色 `--panel`（浅灰），`padding: 20px 12px 12px`，无描边。
- 顶部 logo：22px 方块，`--accent` 底 + `--r-inset`；右侧产品名 15px/700 + `.ws-sub` 12px 灰。
- **分组标题**：`.nav-group-title` 12px/500，`--text-ghost`，不可点击（`cursor: default`），首个分组上边距 16px、其余 24px。
- **选中态 = 整行蓝胶囊**：`.item.on` 背景 `--accent`、字 `--on-accent`、`--shadow-nav-on`；行高 40px、圆角 `--r-ctl`、内距 `0 10px`。
- 底部：主题分段控件 `.seg`（轨道 `--panel-hover`、激活项 `--bg-elev` 白底）+ 品牌卡 `.brand-card`（白底 + 蓝色盾图标 + 「记忆只留在本机，不出网」）。
- ≤900px：侧栏收成 **64px 图标条**（隐藏文字与分组标题，段控件转竖排），`.main` 的 `margin-left` 相应改 88px、padding 改 `16px 14px 32px`。

### 主区必须铺满

```css
.main {
  margin-left: calc(var(--nav-w) + 36px);  /* 232 + 12 左白边 + 24 */
  padding: 24px 24px 40px;
  max-width: none;                          /* 禁止设上限 */
}
```

- **`.main` 一律不设 `max-width`**，左对齐贴侧栏，右边只留自身 padding（24px，窄屏 14px）—— **右侧留白 ≤48px**。
- 页头 `.page-head` 统一由面包屑行 + 大标题组成：面包屑 13px（`--text-faint` + 分隔符 `--text-ghost` + 当前页 `--text-dim`），标题 27px/700。
- 登录 / 初始化 / token 三页不套外壳（`App.vue` 的 `BARE_PAGES`），单独居中排版。

### 卡片网格：自适应列，禁止写死列数

```css
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  align-items: stretch;   /* 等高的地基 */
  gap: 20px;
}
```

- 列数由可用宽度自适应，**禁止写死 `repeat(3, …)` 之类**，否则宽屏右侧空一大块。
- 统计网格同理用 `repeat(auto-fit, minmax(190px, 1fr))`。
- 断点只往下收，**窄屏不出现横向滚动**：
  | 断点 | 变化 |
  |---|---|
  | ≤1200px | `minmax(320px, 1fr)`（列更少但每列更宽） |
  | ≤900px | 单列 `minmax(0, 1fr)`，gap 16px |
  | ≤600px | gap 12px |

---

## 组件规范

### 卡片（`ItemCard.vue`，四段固定顺序）

**顺序不可调换**：
1. **顶行** `.card-top` —— 左侧 `.chip`（可带 `chip--accent` / `chip--danger` 底色）+ 右侧 `.card-time`（12px `--text-faint`）
2. **标题与摘要** —— `.card-title`（16px/600，最多 2 行）+ `.card-body`（14px/1.6 `--text-dim`，最多 3 行）
3. **标签组** `.card-tags` —— `.tag` 描边式 chip，可换行
4. **底部操作行** `.card-foot` —— 上边线 `1px var(--line-3)`、`margin-top: auto` 压到卡底；左侧文字链接 `.card-link`（蓝色 13px/500），右侧 `.card-acts` 里的 `.act-btn`

**等高三件套**（缺一个就静默退化）：`.card-grid { align-items: stretch }` + `.card { height: 100%; flex-direction: column }` + `.card-foot { margin-top: auto }`。
卡内**禁止**出现 `align-self: start` —— 那等于把 stretch 又关掉。

`.card` 本体：padding `20px`、gap `12px`、底 `--bg-elev`、描边 `--border`、圆角 16px、`--shadow-card`；hover 描边升 `--border-2` + 阴影升 `--shadow-hover`。

### 按钮

| 变体 | 配方 | 用在哪 |
|---|---|---|
| **主** | 实心蓝：`--accent` 底 + `--on-accent` 字，hover → `--accent-d2` | 页面唯一主动作、保存/执行 |
| **次** | 白底描边：`--bg-elev` + `1px --border-2` + `--text-dim`，hover → 底 `--chip`、字 `--text` | 取消、返回、列表里的一般动作 |
| **软** | 浅蓝底：`--accent-soft` + `--accent` 字，hover 加 `--accent` 描边 | 次强调（筛选区、保存草稿类） |
| **危险** | 浅红底：`--danger-soft` + `--danger` 字、无描边，hover 描边变 `--danger` | 卡片上的删除/归档按钮 |
| **幽灵** | 无底无框（`.el-button.is-link` / `.is-text`），仅蓝色文字 | 卡片底部的文字链接 |

统一：`--r-sm`（8px）、13px/500、**无阴影**（`box-shadow: none`）、高度 40px（`.el-button` 默认）/ 32px（`.ghost-btn`、`.soft-btn`）/ 28px（`.act-btn`）。
禁用态统一 `opacity: 0.5` + `cursor: not-allowed`，**禁用时不要只改颜色**。

### 输入与下拉

- 高度 **40px**，圆角 **`--r-ctl`（10px）**，白底 `--bg-elev`，描边 **`1px #e8e8ea`（`--border-2`，用 inset box-shadow 实现）**。
- hover：描边转 `--text-ghost`；focus：描边转 `--accent`；`:focus-visible` 另有 `2px solid var(--accent)` 外轮廓。
- 字号 14px，占位符走 `--el-text-color-placeholder` = `--text-ghost`（装饰色，合规）。
- 筛选区控件宽度：下拉 `.sel` 180px，搜索框 `.grow` `flex: 1 1 260px`。
- 浮层（下拉/toast）：`--bg-elev` 底 + `1px --border-2` + `--r-ctl` + `--shadow-pop`。

### chip 与 tag

- **`.chip`**（实心小粒）：`--chip` 底 + `--text-dim` 字，**圆角 6px（`--r-chip`）**，12px/1.7，内距 `1px 8px`。
- **`.tag`**（描边小粒）：`--bg-elev` 底 + `1px --border-2` + `--text-faint` 字，同为 12px + 圆角 6px，超长 `text-overflow: ellipsis` 单行截断。
- 语义变体：`.chip--accent`（`--accent-soft` / `--accent`）、`.chip--danger`（`--danger-soft` / `--danger`）。
- chip 一律 `white-space: nowrap`；成组的靠容器 `flex-wrap: wrap` 换行。

### 标准分页（`PagerBar.vue`）

**不用 `el-pagination`**（它把三块摊开、留白很大），自己排**一排**：

```
左： 共 N 条 · 每页 [20|50|100]      右： [上一页] 1 … 4 [5] 6 … 20 [下一页] 跳至 [__] 页
```

- 外壳 `.pager`：padding `12px 16px`、圆角 16px、描边 `--border`、`--shadow-card`、13px `--text-faint`，`justify-content: space-between`。
- 页码 `.pg`：28px 高、圆角 6px；当前页 `.on` = `--accent-soft` 底 + `--accent` 字 + 600；禁用 `.pg:disabled` 用 `--text-faint`；省略号 `.pg.gap` 用 `--text-ghost`。
- 页码窗口来自 `utils/format.js` 的 `pageWindow`，**最多 5 个数字 + 2 个省略号**。
- 每页条数固定 **20 / 50 / 100**；「共 N 条」口径来自接口（总数未知时改说「至少 N 条（总数未知）」），**前端不另算**。
- 数字用 `.num`（`font-variant-numeric: tabular-nums`）对齐。
- ≤900px：`.pager` 改 `justify-content: flex-start` 折行。

### 危险确认弹框（`ConfirmDialog.vue`）

**三层防手滑，缺一不可**：

1. **标题左侧 3px 竖条** —— `.hd` 用 `border-left: 3px solid var(--accent)`，`tone="danger"` 时改 `--danger`。一眼看出是不可逆方向的操作。
2. **正文逐条列出后果**（`.conseq` 有序列表），不是一句「确定吗」。
3. **必须键入确认串** —— `expect` 非空时（归档=key 末 8 位，实跑=当前计数），照抄一致才 `armed`，主按钮才解除 `disabled`；**每次打开清空上一次输入**。

弹框本体：宽 440px、`align-center`、**`close-on-click-modal: false`**（误点遮罩不该等于误改数据）、`busy` 期间关掉 ESC 与关闭按钮、圆角 16px、`--bg-elev` 底、`--shadow-pop`。
主按钮在 `tone="danger"` 时是**红实心**，按钮文案是**动词**（「确认执行」）不是「确定」。

### 骨架屏与空态

- **骨架屏**：`<el-skeleton :rows="4~5" animated />`，外面套 `.panel.pad`。颜色走 `--el-skeleton-color` / `--el-skeleton-to-color` = `--skeleton-from` / `--skeleton-to`，shimmer 是这两色之间的动画渐变。**只在「加载中且当前无旧数据」时出现**（`v-if="loading && !items.length"`），否则保留旧内容避免闪烁。
- **空态**：`.empty` —— padding `44px 16px`、居中、13px/1.7 `--text-faint`，配一个 30px 线性图标（`--text-ghost`），正文结构是 `<b>一句人话</b> + 下一步指引`。**禁止「暂无数据」这种干巴巴的措辞**，要写清楚「为什么空 + 接下来点哪里」，例如「常驻层还是空的 → 到『记忆条目』把要每轮注入的记忆点升常驻」。

---

## 双主题

- **`:root` 就是浅色（默认）**；暗色挂在 `:root[data-theme="dark"]`。因此无 JS、html 上还没有 `data-theme` 时，首屏也一定是浅色。
- 主题落点两处（`utils/theme.js`）：
  1. `<html data-theme="light|dark">` —— tokens.css 靠它选令牌；
  2. `<html class="dark">` —— element-plus 自带的深色变量挂在这个 class 上，不跟着翻的话弹层/下拉会留一套亮色底。
- 选择记在 **`localStorage["keepsake.console.theme"]`**，缺省或非法值一律回落 `light`（宁可浅色，也不要一屏没配色的裸 html）。该键与 `keepsake.console.token`（登录令牌）互相独立。
- **切换控件在侧栏底部**（`.seg` 分段控件：浅色 / 深色），`aria-pressed` 同步。
- **`--text-ghost` 只许用于装饰/占位符**：placeholder、分隔符、图标、禁用态描边、侧栏分组标题。它在两套主题下都低于 4.5:1，**绝不许承载可读信息**；时间、标签、提示一律用 `--text-faint`。
- Element Plus 品牌色在 tokens.css 末尾统一接管（`:root, html.dark` 并列写，靠本文件在后覆盖 element-plus 的高特异性暗色变量）。新增第三方组件时**优先复用这一层的映射**，不要再单独改 `--el-*`。

---

## 禁止事项

1. **禁止散落硬编码色值** —— 组件里只许写 `var(--*)`。任何 `#hex` / `rgba()` 出现在 `tokens.css` 之外都是 bug。`tests/ui-style.test.mjs` 会全 `src/` 扫描并让测试变红。（唯一例外：`--nav-w` 之类的长度令牌，以及 `preview*.html` 这种不参与构建的静态示意文件。）
2. **禁止写死网格列数** —— 必须 `repeat(auto-fill, minmax(300px, 1fr))` 这类自适应写法；写死 `repeat(3, 1fr)` 会在宽屏右侧留大片空白。
3. **禁止给 `.main` 设 `max-width`** —— 主区必须铺满可用宽度（右侧留白 ≤48px）。居中留白是营销页的做法，不是控制台的。
4. **禁止改 `.card` 内的 `align-self`** —— 会破坏 `stretch` + `height:100%` + `margin-top:auto` 的等高三件套。
5. **禁止新增档外圆角/字号** —— 圆角只用 20/16/12/10/8/6，字号只用 11/12/13/14/16（展示档 22/27/32 除外），且最小 11px。
6. **禁止用 `--text-ghost` 承载要读的文字**。
7. **禁止复刻第三方 logo、专有字体与商标性表述** —— `web/docs/design-reference/`（`sentry.md` / `linear.app.md`，MIT）**只可借鉴结构，不得照抄内容**：它们是营销页视角、无浅色主题、容器写死 1152px。与本文件冲突时以本文件为准。
8. **禁止写死业务数字** —— 总条数、命中率、预算占比等一律来自接口/配置，样式与布局里不许出现业务常量。
9. **禁止改接口层与路由层** —— 纯 UI 改动不得碰 `web/src/api/index.js` 与 `web/src/router/index.js`（diff 必须为 0），也不许在 `.vue` 里加/改业务逻辑。
10. **禁止原生 `alert` / `confirm`** —— 一律用 `utils/dialog.js` 的 `showToast` / `showConfirm`；弹框 `close-on-click-modal=false`。
11. **界面文案禁止再出现「碎片」** —— 术语只有**常驻层 / 记忆条目 / 记忆维护 / 知识库 / 看板**。`tests/ui-style.test.mjs` 会扫 `src/` 断言这一点。
12. **禁止引入外部字体/CDN** —— `web/preview*.html` 必须自包含、双击能开、不联网。

---

## 给 Agent 的执行句式

改 UI 时按这个顺序，不要跳步：

1. **先读本文件**，再看 `web/preview.html`（暗色看 `web/preview-dark.html`）对一眼实物；需要色值时以 `web/src/styles/tokens.css` 为准。
2. **只改样式与展示组件** —— `.vue` 里的 `<style>`、模板结构与 class 名；**不动业务逻辑、不碰接口层与路由层**。
3. **数值逐条从 tokens.css / App.vue / 组件里核对后写**，不许凭印象；本文件与代码冲突时以代码为准，并在报告里列出冲突点。
4. **自查 GATES**（全绿才算完成）：
   ```bash
   # G1 对比度与颜色纪律：theme + ui-style 断言（注意：node --test 不接受目录参数，
   #    必须写文件 glob 或直接裸跑，否则报 Cannot find module 'tests'）
   node --test                     # 在 web/ 下裸跑，自动发现 tests/*.test.mjs（24 条）
   # G2 颜色不散落：src 里除 tokens.css 外没有 #hex / rgba()
   grep -rnE "#[0-9a-fA-F]{3,8}\b|rgba?\(" web/src --include=*.vue | grep -v tokens.css   # 应为 0
   # G3 接口与路由零改动
   git diff --stat -- web/src/api/index.js web/src/router/index.js                      # 应为空
   # G4 构建产物大小
   cd web && npm run build && du -sh dist                                              # dist ≤ 1.5MB
   ```
5. **回传报告**：改了什么 / 为什么 → `git diff` 证据 → GATES 自检输出原文 → 与本文件冲突的点逐条列出。
