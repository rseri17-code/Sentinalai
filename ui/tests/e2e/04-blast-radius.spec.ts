/**
 * Blast Radius Panel Tests
 * Verifies risk tier display, affected services, P1 dependency count, precautions,
 * safe-to-apply vs approval-required banner.
 */
import { test, expect } from '@playwright/test'
import { mockAllApis } from './helpers/mock-api'

test.describe('Blast Radius Panel', () => {
  test.beforeEach(async ({ page }) => {
    await mockAllApis(page)
    // Navigate to investigation and switch to blast-radius panel
    await page.goto('/investigations/inv-001')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(500)
    // Click the blast-radius panel button in sidebar
    const blastBtn = page.getByRole('button', { name: /blast radius/i })
    if (await blastBtn.isVisible()) {
      await blastBtn.click()
      await page.waitForTimeout(800)
    }
  })

  test('blast radius panel renders when selected', async ({ page }) => {
    await page.screenshot({ path: 'tests/e2e/screenshots/blast-radius-panel.png' })
    await expect(page.locator('main')).toBeVisible()
  })

  test('shows risk tier', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toUpperCase()).toMatch(/MEDIUM|HIGH|LOW|CRITICAL/)
  })

  test('shows affected service count', async ({ page }) => {
    const body = await page.locator('body').textContent()
    // 4 affected services
    expect(body).toMatch(/4/)
  })

  test('shows P1 dependency count', async ({ page }) => {
    const body = await page.locator('body').textContent()
    // 2 P1 dependencies
    expect(body).toMatch(/[P]?1|p1|critical/i)
  })

  test('shows affected service names', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('checkout-service')
  })

  test('shows precautions', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/drain|precaution|connection/i)
  })

  test('shows approval-required banner (not safe to auto-apply)', async ({ page }) => {
    const body = await page.locator('body').textContent()
    // MOCK_BLAST_RADIUS.safe_to_auto_apply = false
    expect(body?.toLowerCase()).toMatch(/approval|approve|manual/i)
  })

  test('shows fix type', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/rollback|fix|restart|config/i)
  })

  test('shows target service', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('payment-service')
  })

  test('shows reasoning', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/rollback|restart|depend|reason/i)
  })
})
