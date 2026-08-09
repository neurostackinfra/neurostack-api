import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for the API-provider settings E2E suite.
 *
 * Determinism knobs — locale, timezone, reduced motion, and a pinned viewport
 * per project — are set so baseline PNGs remain stable across machines.
 *
 * Reports, traces, and test-results MUST land in `/tmp/neurostack-playwright`
 * so the repository never accumulates transient artifacts. Only the committed
 * snapshot PNGs under `tests/e2e/*.spec.ts-snapshots/` are tracked.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  outputDir: "/tmp/neurostack-playwright/test-results",
  reporter: [
    ["list"],
    ["html", { outputFolder: "/tmp/neurostack-playwright/reports", open: "never" }],
  ],
  snapshotPathTemplate: "{testDir}/{testFileName}-snapshots/{arg}{ext}",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  use: {
    baseURL: "http://localhost:3001",
    locale: "pt-BR",
    timezoneId: "America/Sao_Paulo",
    reducedMotion: "reduce",
    screenshot: "only-on-failure",
    trace: "off",
    video: "off",
  },
  projects: [
    {
      name: "desktop",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 1000 },
      },
    },
    {
      name: "mobile",
      use: {
        ...devices["Pixel 5"],
        viewport: { width: 390, height: 844 },
      },
    },
  ],
  webServer: {
    command: "PORT=3001 BASE_PATH=/ pnpm dev",
    port: 3001,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
