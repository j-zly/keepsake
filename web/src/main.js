import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
// element-plus 暗色变量（挂 html.dark）+ 本项目设计令牌（令牌表在 tokens.css，组件里只准用 var(--*)）
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles/tokens.css'

import App from './App.vue'
import router from './router'
import { applyTheme, readTheme } from './utils/theme'

// 首屏取一次本地选择；缺省就是 light（tokens.css 的 :root 本身也是浅色）
applyTheme(readTheme())

createApp(App).use(router).use(ElementPlus, { locale: zhCn }).mount('#app')
