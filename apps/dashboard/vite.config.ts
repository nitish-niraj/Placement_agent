import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// base './' keeps the built SPA mountable at "/" of any host (hash router).
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: { proxy: { "/api": "http://localhost:8000" } },
});
