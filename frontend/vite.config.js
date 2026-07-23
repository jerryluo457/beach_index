import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The frontend talks to FastAPI on :8000. Proxying /api in dev keeps api.js free
// of environment branching — the same relative paths work in dev and behind a
// reverse proxy in production.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
