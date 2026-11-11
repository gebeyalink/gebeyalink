import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API is served from /v1 in production behind the same origin. Local development uses
// VITE_API_BASE_URL (see .env.example); the proxy is a convenience for a relative base URL.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
