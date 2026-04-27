import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './tests/e2e',
  outputDir: './tests/e2e/results',
  fullyParallel: false,
  retries: 1,
  workers: 1,
  reporter: [['list'], ['html', { outputFolder: 'tests/e2e/report', open: 'never' }]],
  use: {
    baseURL: 'http://localhost:5173',
    headless: true,
    screenshot: 'on',
    video: 'off',
    viewport: { width: 1440, height: 900 },
    extraHTTPHeaders: { 'x-test-mode': '1' },
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
        channel: undefined,
      },
    },
  ],
  // Dev server already running — don't start it here
})
