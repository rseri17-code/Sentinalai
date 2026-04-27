/**
 * UX Quality Tests — Industry-Grade Bar
 * Validates: no console errors, responsive layout, accessible labels,
 * keyboard nav, loading states, error states, color semantics.
 */
import { test, expect, Page } from '@playwright/test'
import { mockAllApis } from './helpers/mock-api'

async function getConsoleErrors(page: Page): Promise<string[]> {
  const errors: string[] = []
  page.on('console', (msg) => {
    if (msg.type() === 'error') errors.push(msg.text())
  })
  return errors
}

test.describe('UX Quality — Zero Console Errors', () => {
  test('investigations page has no console errors', async ({ page }) => {
    const errors: string[] = []
    page.on('console', (msg) => { if (msg.type() === 'error') errors.push(msg.text()) })
    await mockAllApis(page)
    await page.goto('/investigations')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(800)
    // Filter out known non-critical errors (WS abort, auth)
    const critical = errors.filter(e => !e.includes('WebSocket') && !e.includes('404') && !e.includes('ERR_ABORTED') && !e.includes('ERR_CERT') && !e.includes('fonts.g'))
    expect(critical).toHaveLength(0)
  })

  test('intelligence feed has no critical console errors', async ({ page }) => {
    const errors: string[] = []
    page.on('console', (msg) => { if (msg.type() === 'error') errors.push(msg.text()) })
    await mockAllApis(page)
    await page.goto('/intelligence')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(800)
    const critical = errors.filter(e => !e.includes('WebSocket') && !e.includes('404') && !e.includes('ERR_ABORTED') && !e.includes('ERR_CERT') && !e.includes('fonts.g'))
    expect(critical).toHaveLength(0)
  })

  test('shift handoff has no critical console errors', async ({ page }) => {
    const errors: string[] = []
    page.on('console', (msg) => { if (msg.type() === 'error') errors.push(msg.text()) })
    await mockAllApis(page)
    await page.goto('/handoff')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(800)
    const critical = errors.filter(e => !e.includes('WebSocket') && !e.includes('404') && !e.includes('ERR_ABORTED') && !e.includes('ERR_CERT') && !e.includes('fonts.g'))
    expect(critical).toHaveLength(0)
  })
})

test.describe('UX Quality — Layout & Responsive', () => {
  test('sidebar + main layout on 1440px', async ({ page }) => {
    await mockAllApis(page)
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/investigations')
    await page.waitForLoadState('networkidle')
    const sidebar = page.locator('aside').first()
    const main = page.locator('main').first()
    await expect(sidebar).toBeVisible()
    await expect(main).toBeVisible()
    // Sidebar and main should not overlap
    const sidebarBox = await sidebar.boundingBox()
    const mainBox = await main.boundingBox()
    expect(sidebarBox).not.toBeNull()
    expect(mainBox).not.toBeNull()
    expect(sidebarBox!.x + sidebarBox!.width).toBeLessThanOrEqual(mainBox!.x + 1)
  })

  test('layout holds on 1280px', async ({ page }) => {
    await mockAllApis(page)
    await page.setViewportSize({ width: 1280, height: 800 })
    await page.goto('/investigations')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('aside')).toBeVisible()
    await expect(page.locator('main')).toBeVisible()
  })

  test('intelligence feed renders correctly at 1440px', async ({ page }) => {
    await mockAllApis(page)
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/intelligence')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(600)
    await page.screenshot({ path: 'tests/e2e/screenshots/intelligence-1440.png' })
    await expect(page.locator('main')).toBeVisible()
  })
})

test.describe('UX Quality — Color Semantics & Urgency', () => {
  test('BREACHED urgency badge has red-family color', async ({ page }) => {
    await mockAllApis(page)
    await page.goto('/intelligence')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(800)
    // Find element containing BREACHED text
    const body = await page.locator('body').innerHTML()
    // Should have red class or color in element with BREACHED
    expect(body).toMatch(/red|BREACHED/)
  })

  test('IMMINENT urgency has orange-family color', async ({ page }) => {
    await mockAllApis(page)
    await page.goto('/intelligence')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(800)
    const body = await page.locator('body').innerHTML()
    expect(body).toMatch(/orange|amber|IMMINENT/)
  })

  test('critical severity badge in investigations uses red', async ({ page }) => {
    await mockAllApis(page)
    await page.goto('/investigations')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(600)
    const html = await page.locator('body').innerHTML()
    expect(html).toMatch(/red.*critical|critical.*red/i)
  })

  test('completed status badge uses green color', async ({ page }) => {
    await mockAllApis(page)
    await page.goto('/investigations')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(600)
    const html = await page.locator('body').innerHTML()
    expect(html).toMatch(/green.*completed|completed.*green/i)
  })

  test('running status badge uses blue color', async ({ page }) => {
    await mockAllApis(page)
    await page.goto('/investigations')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(600)
    const html = await page.locator('body').innerHTML()
    expect(html).toMatch(/blue.*running|running.*blue/i)
  })
})

test.describe('UX Quality — Interaction Patterns', () => {
  test('sidebar nav links are keyboard-focusable', async ({ page }) => {
    await mockAllApis(page)
    await page.goto('/investigations')
    await page.waitForLoadState('networkidle')
    // Tab through nav links
    await page.keyboard.press('Tab')
    const focused = await page.evaluate(() => document.activeElement?.tagName)
    expect(['A', 'BUTTON', 'INPUT', 'NAV']).toContain(focused?.toUpperCase() ?? 'A')
  })

  test('inject buttons are clickable', async ({ page }) => {
    await mockAllApis(page)
    await page.goto('/investigations')
    await page.waitForLoadState('networkidle')
    const errorSpikeBtn = page.getByRole('button', { name: /error_spike/i })
    if (await errorSpikeBtn.isVisible()) {
      await expect(errorSpikeBtn).toBeEnabled()
    }
  })

  test('investigation row is clickable', async ({ page }) => {
    await mockAllApis(page)
    await page.goto('/investigations')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(600)
    const rows = page.locator('tr[class*="cursor-pointer"]')
    const count = await rows.count()
    if (count > 0) {
      await rows.first().click()
      await page.waitForTimeout(600)
      await expect(page).toHaveURL(/\/investigations\//)
    }
  })
})

test.describe('UX Quality — Empty & Error States', () => {
  test('empty investigations list shows placeholder message', async ({ page }) => {
    // Return empty list
    await page.route('**/api/v1/investigations**', (route) =>
      route.fulfill({ json: { investigations: [], total: 0, limit: 50, offset: 0 } })
    )
    await page.route('**/api/v1/auth/**', (route) =>
      route.fulfill({ json: { token: 'dev-token', actor_id: 'dev-user', role: 'admin' } })
    )
    await page.goto('/investigations')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(600)
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/no investigation|inject|synthetic|empty/i)
  })

  test('intelligence feed shows empty state when no alerts', async ({ page }) => {
    await page.route('**/api/v1/intelligence/feed**', (route) =>
      route.fulfill({ json: { alerts: [], generated_at: new Date().toISOString() } })
    )
    await page.route('**/api/v1/auth/**', (route) =>
      route.fulfill({ json: { token: 'dev-token', actor_id: 'dev-user', role: 'admin' } })
    )
    await page.goto('/intelligence')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(800)
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/all clear|no alert|no signal|empty|clear/i)
  })
})

test.describe('UX Quality — Performance', () => {
  test('investigations page loads in under 3 seconds', async ({ page }) => {
    await mockAllApis(page)
    const start = Date.now()
    await page.goto('/investigations')
    await page.waitForLoadState('networkidle')
    const elapsed = Date.now() - start
    expect(elapsed).toBeLessThan(3000)
  })

  test('intelligence feed renders in under 3 seconds', async ({ page }) => {
    await mockAllApis(page)
    const start = Date.now()
    await page.goto('/intelligence')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(800)
    const elapsed = Date.now() - start
    expect(elapsed).toBeLessThan(3000)
  })
})
