import { defineConfig, devices } from '@playwright/test'

/**
 * Visual regression against the real frontend and the real BFF in synthetic
 * mode -- no hand-written API fixtures to drift, and no production data.
 *
 * Every UI release used to end with the user refreshing and checking by eye;
 * design-qa.md records "final result: blocked" for each one because the agent
 * cannot drive the user's browser. This runs a browser inside the test process
 * instead, which is a different thing: it never touches the user's desktop,
 * session or production data.
 */
export default defineConfig({
  testDir: './visual',
  snapshotPathTemplate: '{testDir}/__screenshots__/{arg}-{projectName}{ext}',
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: process.env.CI ? 'github' : 'list',
  expect: {
    toHaveScreenshot: {
      // Font rasterisation differs by a hair between machines; a real layout or
      // type-scale regression moves far more than this.
      maxDiffPixelRatio: 0.01,
      animations: 'disabled',
    },
  },
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 960 } } },
    { name: 'laptop', use: { ...devices['Desktop Chrome'], viewport: { width: 1180, height: 820 } } },
  ],
  webServer: {
    command: 'node scripts/serve-preview.mjs',
    url: 'http://127.0.0.1:4173/api/v1/auth/status',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
})
