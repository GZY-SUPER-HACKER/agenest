import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    // The backend serves these as static files; a stable name keeps the deploy diff small.
    outDir: "dist",
    assetsDir: "assets",
    sourcemap: false,
  },
  server: {
    port: 5173,
    // Proxy the API in dev so `npm run dev` works without CORS or a token prompt.
    proxy: {
      "/api": {
        target: process.env.VITE_API_ORIGIN ?? "http://127.0.0.1:8780",
        changeOrigin: false,
      },
    },
  },
});
