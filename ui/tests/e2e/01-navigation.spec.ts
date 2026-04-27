/**
 * Navigation & Shell Tests
 * Verifies the app loads, sidebar renders, routing works between all pages.
 */
import { test, expect } from '@playwright/test'
import { mockAllApis } from './helpers/mock-api'

test.describe('App Shell & Navigation', () => {
  test.beforeEach(async ({ page }) => {
    await mockAllApis(page)
  })

  test('loads and shows sidebar logo', async ({ page }) => {
    await page.goto('/')
    await page.waitForLoadState('networkidle')
    await expect(page.locator('aside')).toBeVisible()
    // Logo area
    await expect(page.locator('aside').first()).toContainText('ObserveAI')
  })

  test('sidebar shows all top-level nav items', async ({ page }) => {
    await page.goto('/investigations')
    await page.waitForLoadState('networkidle')
    await expect(page.getByText('All Investigations')).toBeVisible()
    await expect(page.getByText('Intelligence Feed')).toBeVisible()
    await expect(page.getByText('Shift Handoff')).toBeVisible()
  })

  test('navigates to Intelligence Feed', async ({ page }) => {
    await page.goto('/intelligence')
    await page.waitForLoadState('networkidle')
    await page.screenshot({ path: 'tests/e2e/screenshots/intelligence-feed.png' })
    // Page should render (not crash)
    await expect(page.locator('main')).toBeVisible()
  })

  test('navigates to Shift Handoff', async ({ page }) => {
    await page.goto('/handoff')
    await page.waitForLoadState('networkidle')
    await page.screenshot({ path: 'tests/e2e/screenshots/shift-handoff.png' })
    await expect(page.locator('main')).toBeVisible()
  })

  test('investigations list loads', async ({ page }) => {
    await page.goto('/investigations')
    await page.waitForLoadState('networkidle')
    await page.screenshot({ path: 'tests/e2e/screenshots/investigations-list.png' })
    await expect(page.locator('main')).toBeVisible()
  })

  test('dark theme is applied', async ({ page }) => {
    await page.goto('/')
    const html = await page.locator('html')
    const bgColor = await page.evaluate(() =>
      window.getComputedStyle(document.querySelector('body')!).backgroundColor
    )
    // Slate-950 is very dark — not white
    expect(bgColor).not.toBe('rgb(255, 255, 255)')
  })

  test('sidebar WS status indicator present', async ({ page }) => {
    await page.goto('/investigations')
    await page.waitForLoadState('networkidle')
    // Status dot exists in sidebar footer
    await expect(page.locator('aside .rounded-full')).toBeVisible()
  })

  test('unknown route redirects to investigations', async ({ page }) => {
    await page.goto('/unknown-route-xyz')
    await page.waitForLoadState('networkidle')
    await expect(page).toHaveURL(/\/investigations/)
  })
})
