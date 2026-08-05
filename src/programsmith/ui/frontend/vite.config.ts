import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The Python backend (FastAPI) serves the built SPA from `frontend/dist` at the
// site root and exposes the JSON API under `/api`. In dev we proxy `/api` to the
// running backend so the same-origin fetch("/api/...") calls work unchanged.
export default defineConfig({
  base: "/",
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true,
      },
    },
  },
});
