import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built into the Python package so `djlib serve` ships the UI.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../src/djlib/web", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});
