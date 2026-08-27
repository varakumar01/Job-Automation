import path from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { tunnelGate } from './tunnel-gate'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const tunnelMode = !!process.env.TUNNEL_TOKEN

  return {
    plugins: [react(), tailwindcss(), tunnelGate()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src'),
      },
    },
    server: {
      port: 5178,
      allowedHosts: tunnelMode ? ['.trycloudflare.com'] : [],
      hmr: tunnelMode
        ? process.env.TUNNEL_HMR
          ? { protocol: 'wss', clientPort: 443 }
          : false
        : true,
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
  }
})
