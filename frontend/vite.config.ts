import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/health': 'http://127.0.0.1:8000',
      '/system': 'http://127.0.0.1:8000',
      '/scans': 'http://127.0.0.1:8000',
      '/wallets': 'http://127.0.0.1:8000',
      '/signals': 'http://127.0.0.1:8000',
      '/paper': 'http://127.0.0.1:8000',
      '/positions': 'http://127.0.0.1:8000',
      '/portfolio': 'http://127.0.0.1:8000',
      '/decision-log': 'http://127.0.0.1:8000',
      '/experiments': 'http://127.0.0.1:8000',
      '/data': 'http://127.0.0.1:8000',
      '/config': 'http://127.0.0.1:8000',
      '/idempotency': 'http://127.0.0.1:8000',
    },
  },
})
