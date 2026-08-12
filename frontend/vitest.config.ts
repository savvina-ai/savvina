import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { fileURLToPath, URL } from 'node:url';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    environmentOptions: {
      jsdom: {
        url: 'http://localhost:8000',
      },
    },
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    // Must stay above the asyncUtilTimeout set in src/test/setup.ts, so a query
    // that never resolves surfaces as "unable to find element" rather than an
    // opaque test timeout with no DOM dump.
    testTimeout: 15000,
    hookTimeout: 15000,
  },
});
