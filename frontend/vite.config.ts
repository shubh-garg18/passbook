import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The bundle is copied into the Python image and served by Flask from
// src/passbook/web/dist. Same origin, so there is no CORS configuration and
// no API base URL to get wrong.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: '../src/passbook/web/dist',
    emptyOutDir: true,
    // Fonts are 4-10 KB each after subsetting; inlining them would duplicate
    // them into every CSS rebuild and lose the year-long cache headers Flask
    // sets on /assets. Keep them as files.
    assetsInlineLimit: 2048,
  },
  server: {
    port: 5173,
    // `npm run dev` against a locally running container. Production never uses
    // this path — Flask serves the built files itself.
    proxy: { '/api': { target: 'http://127.0.0.1:8081', changeOrigin: false } },
  },
})
