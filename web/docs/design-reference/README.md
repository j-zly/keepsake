# design-reference — 第三方风格锚点（仅内部参考）

这里放的是从 [VoltAgent/awesome-design-md](https://github.com/VoltAgent/awesome-design-md)（**MIT**）原样抓取的两份 `DESIGN.md`，用来在需要换风格时做对照。

| 文件 | 来源 raw 地径 | 抓取日期 | 备注 |
|---|---|---|---|
| `sentry.md` | `raw.githubusercontent.com/VoltAgent/awesome-design-md/main/design-md/sentry/DESIGN.md` | 2026-10-14 | 深紫罗兰画布 + 柠檬绿点缀；**双极画布**（暗色营销页 / 白底密集表格）写得最好 |
| `linear.app.md` | `…/design-md/linear.app/DESIGN.md` | 2026-10-14 | 近黑 `#010102` + 淡紫蓝 `#5e6ad2` 单强调；**官方站没有浅色主题**（原文明确说明） |

## ⚠️ 使用约束（务必先读）
1. **它们描述的是「品牌官网」，不是产品后台**：通篇 marketing hero / pricing / 截图面板，容器还写死 1152px、Linear 那份直接没有浅色 —— **不要**照搬到控制台。
2. **不是可直接复制的令牌表**：里面是 `{colors.accent-violet-mid}` 这类占位变量，落地要自己定值；头部 `version: alpha`，且原文自述 as-is、不主张所有权。
3. **品牌资产**：仅作内部风格分析，界面里**不得**复刻其 logo、专有字体与商标性表述。
4. 控制台的**权威规范只有一份：[`web/DESIGN.md`](../../DESIGN.md)** —— 那是我们自己三轮验收过的令牌与规则，与本目录冲突时**以 `web/DESIGN.md` 为准**。
