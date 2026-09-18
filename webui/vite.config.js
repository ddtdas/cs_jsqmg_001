import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// WebUI 只是主控 REST API 的客户端（D1：不在 WebUI 复制业务逻辑）。
// - dev: vite 代理 /api -> 主控 127.0.0.1:9200（与后端前缀 /api/v1 对齐，无路径重写）
// - build: base '/ui/' 对齐 FastAPI 静态托管 http://host:9200/ui/（见 start.bat 注释）
export default defineConfig({
  plugins: [vue()],
  base: '/ui/',
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:9200',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        manualChunks: {
          'vendor-vue': ['vue', 'vue-router', 'pinia'],
          'vendor-element': ['element-plus'],
          'vendor-echarts': ['echarts'],
        },
      },
    },
  },
})
