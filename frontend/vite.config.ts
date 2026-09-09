import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

/**
 * Куда dev-сервер проксирует `/api`, `/ws` и `/video`.
 *
 * По умолчанию — мок на 8080 (`npm run mock`). Переменная нужна, когда 8080
 * уже занят настоящим шлюзом из `deploy/`: тогда мок поднимают на другом порту
 * (`PORT=8081 npm run mock`) и запускают `MOCK_URL=http://localhost:8081 npm run dev`.
 */
const MOCK = process.env.MOCK_URL ?? 'http://localhost:8080';

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': { target: MOCK, changeOrigin: true },
      '/video': { target: MOCK, changeOrigin: true },
      '/mock': { target: MOCK, changeOrigin: true },
      '/ws': { target: MOCK.replace('http', 'ws'), ws: true },
    },
  },
  build: {
    target: 'es2020',
    sourcemap: true,
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
    globals: false,
  },
});
