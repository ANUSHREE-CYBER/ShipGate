import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Built output is served by FastAPI from frontend/dist, so the app and the API
// share an origin in production and no CORS config is needed. The proxy below
// only matters during `npm run dev`, where Vite serves the UI on its own port.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/orders': 'http://127.0.0.1:8000',
      '/risk': 'http://127.0.0.1:8000',
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
