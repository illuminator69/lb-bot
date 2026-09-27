import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      // LB_API points the dev server at another lb-bot, e.g. the NAS:
      //   LB_API=http://192.168.129.153:8899 npm run dev
      '/api': process.env.LB_API || 'http://localhost:8899',
    },
  },
})
