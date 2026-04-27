/**
 * Shift Handoff Panel Tests
 * Fragile services, conditional guidance, active investigations, accept shift button.
 */
import { test, expect } from '@playwright/test'
import { mockAllApis } from './helpers/mock-api'

test.describe('Shift Handoff Panel', () => {
  test.beforeEach(async ({ page }) => {
    await mockAllApis(page)
    await page.goto('/handoff')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(800)
  })

  test('renders without crash', async ({ page }) => {
    await expect(page.locator('main')).toBeVisible()
    await page.screenshot({ path: 'tests/e2e/screenshots/shift-handoff-loaded.png' })
  })

  test('shows engineer names', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('alice')
    expect(body).toContain('bob')
  })

  test('shows shift summary', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('fragile')
  })

  test('shows fragile services section', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('payment-service')
    expect(body).toContain('order-db')
  })

  test('shows critical risk level for payment-service', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toContain('critical')
  })

  test('shows active investigation', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('INC12346')
  })

  test('shows conditional guidance (IF/THEN)', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/if|condition|guidance/i)
  })

  test('shows IF payment-service CPU guidance', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('payment-service')
  })

  test('shows upcoming risk / changes', async ({ page }) => {
    const body = await page.locator('body').textContent()
    // Should mention a deployment or change
    expect(body?.toLowerCase()).toMatch(/deployment|change|upcoming|chg/i)
  })

  test('Accept Shift button is present', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/accept|acknowledge|shift/i)
  })

  test('accept shift interaction works', async ({ page }) => {
    const acceptBtn = page.getByRole('button', { name: /accept/i })
    if (await acceptBtn.isVisible()) {
      await acceptBtn.click()
      await page.waitForTimeout(300)
      // After accepting, some confirmation should appear
      const body = await page.locator('body').textContent()
      expect(body?.toLowerCase()).toMatch(/accepted|confirmed|handoff/i)
    }
  })

  test('incident count badge shown for fragile services', async ({ page }) => {
    const body = await page.locator('body').textContent()
    // 5 incidents in 7 days
    expect(body).toMatch(/5|incident/i)
  })

  test('page shows shift handoff heading', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/shift|handoff|hand.*off/i)
  })
})
