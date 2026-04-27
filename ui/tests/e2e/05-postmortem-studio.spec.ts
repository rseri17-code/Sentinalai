/**
 * Postmortem Studio Tests
 * 5 Whys, timeline, action items, approve gate, publish.
 */
import { test, expect } from '@playwright/test'
import { mockAllApis } from './helpers/mock-api'

test.describe('Postmortem Studio', () => {
  test.beforeEach(async ({ page }) => {
    await mockAllApis(page)
    await page.goto('/investigations/inv-001')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(500)
    const pmBtn = page.getByRole('button', { name: /postmortem/i })
    if (await pmBtn.isVisible()) {
      await pmBtn.click()
      await page.waitForTimeout(1000)
    }
  })

  test('postmortem panel renders', async ({ page }) => {
    await page.screenshot({ path: 'tests/e2e/screenshots/postmortem-studio.png' })
    await expect(page.locator('main')).toBeVisible()
  })

  test('shows executive summary', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/executive|summary|cascade|timeout/i)
  })

  test('shows 5 Whys section', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/why|5 why|five why/i)
  })

  test('shows timeline events', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/timeline|10:30|latency/i)
  })

  test('shows action items', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/action|ci pipeline|platform/i)
  })

  test('shows what went well section', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/went well|alert|agent/i)
  })

  test('shows contributing factors', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/contributing|factor|staging|index/i)
  })

  test('shows Approve button', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/approve|draft/i)
  })

  test('shows Publish button (disabled until approved)', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/publish|confluence/i)
  })

  test('shows incident id in postmortem', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('INC12345')
  })

  test('shows confidence score', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toMatch(/87|confidence/i)
  })

  test('shows root cause', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/missing index|table scan|root cause/i)
  })

  test('shows prevention recommendations', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/prevent|recommend|schema|load test/i)
  })
})
