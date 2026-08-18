import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const configuredBase = process.env.VITE_BASE_PATH?.trim()

export default defineConfig({
  base: configuredBase || './',
  plugins: [react()],
  build: {
    sourcemap: true,
  },
})
