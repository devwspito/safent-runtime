import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The SPA is served at /app/ in production (shell-server mounts the dist there).
// In dev mode the Vite server proxies /api/* to the running shell-server so the
// dev loop works without CORS or a separate token-injection step.
export default defineConfig({
  plugins: [react()],
  base: '/app/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // noVNC (@novnc/novnc) uses top-level await → needs es2022+. All target
    // browsers (modern Chrome/Safari/Firefox) support it.
    target: 'es2022',
  },
  optimizeDeps: {
    esbuildOptions: { target: 'es2022' },
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:17517',
        changeOrigin: true,
      },
      '/ws': {
        target: 'ws://127.0.0.1:17517',
        ws: true,
      },
    },
  },
})
