import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'
// 深色变量 + 本项目设计令牌（令牌表在 tokens.css，组件里只准用 var(--*)）
import 'element-plus/theme-chalk/dark/css-vars.css'
import './styles/tokens.css'

import App from './App.vue'
import router from './router'

createApp(App).use(router).use(ElementPlus, { locale: zhCn }).mount('#app')
