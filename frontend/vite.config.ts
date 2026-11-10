import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// The admin console talks to the FastAPI backend through same-origin /api,
// proxied in development and preview, so the session cookie (Path=/api/admin,
// SameSite=Strict) is first-party and no CORS is needed.
const api = process.env.LTD_API_URL ?? 'http://127.0.0.1:8010';

export default defineConfig({
  plugins: [react()],
  server: { host: '127.0.0.1', port: 5173, proxy: { '/api': { target: api } } },
  preview: { host: '127.0.0.1', port: 4173, proxy: { '/api': { target: api } } },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
    testTimeout: 15000,
  },
});
