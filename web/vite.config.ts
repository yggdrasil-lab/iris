import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

const API_TARGET = process.env.IRIS_API_TARGET ?? 'http://localhost:8000';

export default defineConfig({
  cacheDir: 'node_modules/.cache/vite',
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
});
