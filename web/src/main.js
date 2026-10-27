import { createApp } from 'vue'
// Element Plus **按需引入**（2026-10 ks_maint_ui_dedup · G5 产物体积门）：
// 整包引入 + 全局注册会把全库组件 + 全量 CSS 打进产物
// （dist ≈ 2.29 MB，超过 1.6 MB 上限），而这个控制台真正用到的只有
// 下面这十几个组件。改成按需注册后 dist ≈ 1.50 MB。
//
// 🔴 三条纪律：
//   1) **用到的每个组件都要登记**（模板里 <el-xxx> ↔ 这里 ElXxx 一一对应，
//      漏一个 = 那个组件渲染不出来）；tests/ep-on-demand.test.mjs 盯着这件事；
//   2) **每个组件的 CSS 也要单独引**（element-plus 的 es 模块不带样式，
//      样式在 theme-chalk/*.css 里，漏一个 = 组件没样式）；
//   3) 语言包走 provideGlobalConfig（等价于整包安装时的 locale 参数），
//      日期选择器 / 消息框的中文文案不丢。
import {
  ElButton,
  ElCheckbox,
  ElCollapse,
  ElCollapseItem,
  ElDatePicker,
  ElDialog,
  ElDrawer,
  ElForm,
  ElFormItem,
  ElInput,
  ElInputNumber,
  ElOption,
  ElRadioButton,
  ElRadioGroup,
  ElSelect,
  ElSkeleton,
  ElSwitch,
  provideGlobalConfig,
} from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/theme-chalk/base.css'
import 'element-plus/theme-chalk/el-button.css'
import 'element-plus/theme-chalk/el-checkbox.css'
import 'element-plus/theme-chalk/el-checkbox-group.css'
import 'element-plus/theme-chalk/el-collapse.css'
import 'element-plus/theme-chalk/el-collapse-item.css'
import 'element-plus/theme-chalk/el-collapse-transition.css'
import 'element-plus/theme-chalk/el-date-picker.css'
import 'element-plus/theme-chalk/el-date-picker-panel.css'
import 'element-plus/theme-chalk/el-dialog.css'
import 'element-plus/theme-chalk/el-drawer.css'
import 'element-plus/theme-chalk/el-form.css'
import 'element-plus/theme-chalk/el-form-item.css'
import 'element-plus/theme-chalk/el-icon.css'
import 'element-plus/theme-chalk/el-input.css'
import 'element-plus/theme-chalk/el-input-number.css'
import 'element-plus/theme-chalk/el-message.css'
import 'element-plus/theme-chalk/el-message-box.css'
import 'element-plus/theme-chalk/el-option.css'
import 'element-plus/theme-chalk/el-overlay.css'
import 'element-plus/theme-chalk/el-popper.css'
import 'element-plus/theme-chalk/el-radio.css'
import 'element-plus/theme-chalk/el-radio-button.css'
import 'element-plus/theme-chalk/el-radio-group.css'
import 'element-plus/theme-chalk/el-scrollbar.css'
import 'element-plus/theme-chalk/el-select.css'
import 'element-plus/theme-chalk/el-select-dropdown.css'
import 'element-plus/theme-chalk/el-skeleton.css'
import 'element-plus/theme-chalk/el-skeleton-item.css'
import 'element-plus/theme-chalk/el-switch.css'
import 'element-plus/theme-chalk/el-time-picker.css'
import 'element-plus/theme-chalk/el-time-select.css'
import 'element-plus/theme-chalk/el-tooltip.css'
// element-plus 暗色变量（挂 html.dark）+ 本项目设计令牌（令牌表在 tokens.css，组件里只准用 var(--*)）
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles/tokens.css'

import App from './App.vue'
import router from './router'
import { applyTheme, readTheme } from './utils/theme'

// 首屏取一次本地选择；缺省就是 light（tokens.css 的 :root 本身也是浅色）
applyTheme(readTheme())

const app = createApp(App)
// 全局配置（语言包 / 命名空间 / zIndex）：等价于整包安装时的 locale 选项
provideGlobalConfig({ locale: zhCn }, app, true)
app.use(router)
for (const c of [
  ElButton,
  ElCheckbox,
  ElCollapse,
  ElCollapseItem,
  ElDatePicker,
  ElDialog,
  ElDrawer,
  ElForm,
  ElFormItem,
  ElInput,
  ElInputNumber,
  ElOption,
  ElRadioButton,
  ElRadioGroup,
  ElSelect,
  ElSkeleton,
  ElSwitch,
]) {
  app.use(c)
}
app.mount('#app')
