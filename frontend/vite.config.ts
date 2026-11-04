import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In dev (`npm run dev`, port 5173) API calls are proxied to the backend on 9700.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:9700", ws: true },
      "/health": "http://localhost:9700",
    },
  },
});
