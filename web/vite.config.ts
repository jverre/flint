import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by Flint at /ui (daemon: src/flint/daemon/ui, control plane: embedded).
// Dev: `npm run dev` proxies the API to FLINT_API (default: local daemon on :9100).
const API = process.env.FLINT_API ?? "http://127.0.0.1:9100";
const proxied = ["/health", "/vms", "/events", "/templates", "/config", "/volumes", "/v1"];

export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  build: { outDir: process.env.FLINT_UI_OUT ?? "dist", emptyOutDir: true, sourcemap: false },
  server: {
    port: 5173,
    proxy: Object.fromEntries(proxied.map((p) => [p, { target: API, changeOrigin: true, ws: true }])),
  },
});
