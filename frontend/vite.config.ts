/// <reference types="vitest/config" />
import { defineConfig, type ProxyOptions } from 'vite'
import react from '@vitejs/plugin-react'

const backend = 'http://127.0.0.1:8000'

const spaRouteProxy: ProxyOptions = {
  target: backend,
  bypass: (req) => {
    if (req.headers.accept?.includes('text/html')) {
      return '/index.html'
    }
  },
}

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/health': backend,
      '/system': backend,
      '/scans': backend,
      '/wallets': spaRouteProxy,
      '/signals': spaRouteProxy,
      '/paper': backend,
      '/positions': backend,
      '/portfolio': spaRouteProxy,
      '/decision-log': backend,
      '/experiments': backend,
      '/data': backend,
      '/config': backend,
      '/idempotency': backend,
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
})
