import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

// Separate from `vite.config.ts` on purpose — the app config throws when the
// PORT/BASE_PATH environment variables are missing, which would break `vitest`
// runs in CI and local development. This file mirrors only the pieces the
// tests need (React JSX transform + `@` path alias) and nothing else.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
});
