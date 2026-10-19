import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // 前端 :5173 → 后端 :8000
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        // ★ SSE 必须关闭缓冲，否则进度不会实时推送
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (proxyRes.headers['content-type']?.includes('text/event-stream')) {
              proxyRes.headers['cache-control'] = 'no-cache'
              delete proxyRes.headers['content-length']
            }
          })
        },
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
})