import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5178,
    proxy: {
      // Local FastAPI backend (`main.py serve`, default port 8000).
      '/api': 'http://127.0.0.1:8000',
    },
  },
  build: {
    // `npm run build:static` (mode=static) outputs to a separate dir from the
    // full control-panel build, so both can exist side by side if needed.
    outDir: mode === 'static' ? 'dist-static' : 'dist',
  },
}))
