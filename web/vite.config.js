import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 构建产物固定落在 dist/ —— 后端 create_app() 按 web/dist/index.html 挂静态文件。
export default defineConfig({
  plugins: [vue()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
  },
  server: {
    port: 5273,
    proxy: {
      // 本地开发时把 API 代理到后端（生产由后端同源挂载）
      '/api': { target: 'http://127.0.0.1:8211', changeOrigin: false },
      '/health': { target: 'http://127.0.0.1:8211', changeOrigin: false },
    },
  },
})
