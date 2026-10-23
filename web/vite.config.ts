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
    // P3-18：路由已 React.lazy 拆分，恢复默认告警阈值（原 1200 是
    // 为单一 1260KB chunk 抬高阈值掩盖问题）
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        manualChunks: {
          // 体积大户独立分包，利用浏览器缓存（业务代码改动不再让
          // antd/React 整包失效）
          'antd': ['antd', '@ant-design/icons'],
          'react': ['react', 'react-dom', 'react-router-dom'],
        },
      },
    },
  },
})