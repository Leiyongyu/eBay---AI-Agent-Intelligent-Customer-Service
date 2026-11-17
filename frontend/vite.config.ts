import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// npm run build 输出到 dist，由 FastAPI 托管（main.py 启动时自动构建）。
// 前端开发时可 npm run dev（端口 5173），接口代理到后端 8000。
const backend = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': backend,
      '/internal': backend,
      '/dev': backend,
      '/webhooks': backend,
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 2000,
  },
})
