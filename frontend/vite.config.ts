import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: '/console/',
  server: {
    port: 5173,
    proxy: { '/api': 'http://localhost:8000', '/health': 'http://localhost:8000', '/ready': 'http://localhost:8000' },
  },
})

