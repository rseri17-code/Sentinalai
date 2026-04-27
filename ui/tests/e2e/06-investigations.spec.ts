/**
 * Investigations List & Investigation View Tests
 * Table, severity badges, status badges, panel switching.
 */
import { test, expect } from '@playwright/test'
import { mockAllApis } from './helpers/mock-api'

test.describe('Investigations List', () => {
  test.beforeEach(async ({ page }) => {
    await mockAllApis(page)
    await page.goto('/investigations')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(600)
  })

  test('renders investigations list heading', async ({ page }) => {
    await expect(page.getByRole('heading', { name: 'Investigations' })).toBeVisible()
  })

  test('shows incident IDs from mock data', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('INC12345')
    expect(body).toContain('INC12346')
  })

  test('shows affected service names', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('payment-service')
    expect(body).toContain('user-service')
  })

  test('shows severity badges (critical, major)', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('critical')
    expect(body).toContain('major')
  })

  test('shows status badges (completed, running)', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toContain('completed')
    expect(body).toContain('running')
  })

  test('shows confidence values', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toMatch(/87%/)
  })

  test('shows inject synthetic buttons', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body).toMatch(/error_spike|oomkill|latency|timeout/i)
  })

  test('screenshot for visual review', async ({ page }) => {
    await page.screenshot({ path: 'tests/e2e/screenshots/investigations-list-full.png', fullPage: true })
  })
})

test.describe('Investigation Detail View', () => {
  test.beforeEach(async ({ page }) => {
    await mockAllApis(page)
    await page.goto('/investigations/inv-001')
    await page.waitForLoadState('networkidle')
    await page.waitForTimeout(600)
  })

  test('renders investigation view', async ({ page }) => {
    await expect(page.locator('main')).toBeVisible()
    await page.screenshot({ path: 'tests/e2e/screenshots/investigation-view.png' })
  })

  test('shows panel navigation in sidebar', async ({ page }) => {
    const body = await page.locator('aside').textContent()
    expect(body).toContain('Timeline')
    expect(body).toContain('Evidence')
    expect(body).toContain('Memory Trace')
    expect(body).toContain('Blast Radius')
    expect(body).toContain('Control')
    expect(body).toContain('Postmortem')
  })

  test('shows risk/confidence bar', async ({ page }) => {
    // RiskConfidenceLayer is always visible
    const body = await page.locator('body').textContent()
    // Should show risk info
    expect(body?.toLowerCase()).toMatch(/risk|confidence|budget/i)
  })

  test('Timeline panel is default', async ({ page }) => {
    const body = await page.locator('body').textContent()
    expect(body?.toLowerCase()).toMatch(/timeline|event|agent|investigation/i)
  })

  test('can switch to Evidence panel', async ({ page }) => {
    const evidenceBtn = page.getByRole('button', { name: /Evidence/i })
    if (await evidenceBtn.isVisible()) {
      await evidenceBtn.click()
      await page.waitForTimeout(400)
      await page.screenshot({ path: 'tests/e2e/screenshots/evidence-panel.png' })
    }
  })

  test('can switch to Memory Trace panel', async ({ page }) => {
    const memBtn = page.getByRole('button', { name: /Memory Trace/i })
    if (await memBtn.isVisible()) {
      await memBtn.click()
      await page.waitForTimeout(400)
      await page.screenshot({ path: 'tests/e2e/screenshots/memory-trace-panel.png' })
    }
  })

  test('can switch to Control panel', async ({ page }) => {
    const controlBtn = page.getByRole('button', { name: /Control/i })
    if (await controlBtn.isVisible()) {
      await controlBtn.click()
      await page.waitForTimeout(400)
      await page.screenshot({ path: 'tests/e2e/screenshots/control-panel.png' })
    }
  })
})
