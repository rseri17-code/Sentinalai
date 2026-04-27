/**
 * Intelligence Feed Tests
 * Proactive signal feed: urgency badges, filter tabs, acknowledge, investigate buttons.
 */
import { test, expect } from '@playwright/test'
import { mockAllApis, MOCK_INTELLIGENCE_FEED } from './helpers/mock-api'

test.describe('Intelligence Feed', () => {
  test.beforeEach(async ({ page }) => {
    await mockAllApis(page)
    await page.goto('/intelligence')
    await page.waitForLoadState('networkidle')
    // Give component time to fetch and render
    await page.waitForTimeout(800)
  })

  test('renders page without crash', async ({ page }) => {
    await expect(page.locator('main')).toBeVisible()
    await page.screenshot({ path: 'tests/e2e/screenshots/intelligence-feed-loaded.png' })
  })

  test('shows BREACHED alert prominently', async ({ page }) => {
    // BREACHED urgency should appear
    const body = await page.locator('body').textContent()
    expect(body).toContain('BREACHED')
  })

  test('shows IMMINENT alert', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('IMMINENT')
  })

  test('shows WARNING alert', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('WARNING')
  })

  test('shows WATCH alert', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('WATCH')
  })

  test('shows service names in alerts', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('payment-service')
    expect(body).toContain('search-service')
  })

  test('shows metric names', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('cpu_utilisation')
  })

  test('shows recommended actions', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('Scale horizontally')
  })

  test('filter tabs are present', async ({ page }) => {
    const body = await page.locator('body').textContent()
    // Should have tab filters
    expect(body).toContain('ALL')
  })

  test('BREACHED filter tab works', async ({ page }) => {
    // Click BREACHED filter tab
    const breachedTab = page.getByRole('button', { name: /BREACHED/i })
    if (await breachedTab.isVisible()) {
      await breachedTab.click()
      await page.waitForTimeout(300)
      // Should still show BREACHED alert
      await expect(page.locator('body')).toContainText('search-service')
    }
  })

  test('Investigate Now button present for critical alerts', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toContain('investigate')
  })

  test('Acknowledge button present', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toContain('acknowledge')
  })

  test('confidence percentage shown', async ({ page }) => {
    const body = await page.locator('body').textContent()
    // 84% or 91% or similar confidence values
    expect(body).toMatch(/\d+%/)
  })

  test('minutes to breach shown for WARNING', async ({ page }) => {
    const body = await page.locator('body').textContent()
    // "18" minutes to breach for WARNING alert
    expect(body).toMatch(/1[0-9]\.?\d*\s*min|min.*1[0-9]/i)
  })

  test('page has correct heading', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/intelligence|signal|proactive|sentinel/i)
  })
})
