import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 4173,
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test-setup.ts',
    // The visual baselines are Playwright specs driving a real browser; vitest
    // would otherwise collect them and fail on an import it cannot resolve.
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
})
