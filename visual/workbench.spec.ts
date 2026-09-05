import { expect, test, type Page } from '@playwright/test'

/**
 * One baseline per workbench surface.
 *
 * These catch the class of regression that previously only the user's eyes
 * caught: type scale, information density, column structure, and sections that
 * silently stop rendering. They are not a substitute for the user's judgement
 * about whether a design is right -- only for noticing when it changes.
 */

const SURFACES = [
  { name: 'overview', path: '/overview', ready: '早上好，今天有几项需要确认' },
  { name: 'monthly-reconciliation', path: '/reconciliation', ready: '月度对账' },
  { name: 'personal-finance', path: '/personal-finance', ready: '完整个人财务对账' },
  { name: 'company-reports', path: '/company-reports', ready: '各公司报表' },
  { name: 'audit-log', path: '/audit', ready: '审核操作记录' },
] as const

async function settle(page: Page) {
  // Freeze anything that would make a baseline flap: caret blink, spinners,
  // and the relative timestamps that change with the clock.
  await page.addStyleTag({
    content: `*, *::before, *::after {
      animation-duration: 0s !important;
      animation-delay: 0s !important;
      transition-duration: 0s !important;
      transition-delay: 0s !important;
      caret-color: transparent !important;
    }`,
  })
  await page.evaluate(() => document.fonts?.ready)
  await expect(page.getByText('正在读取财务数据')).toHaveCount(0)
}

for (const surface of SURFACES) {
  test(`${surface.name} matches its baseline`, async ({ page }) => {
    await page.goto(surface.path)
    await expect(page.getByText(surface.ready).first()).toBeVisible({ timeout: 20_000 })
    await settle(page)
    await expect(page).toHaveScreenshot(`${surface.name}.png`, { fullPage: true })
  })
}

test('candidate review dialog matches its baseline', async ({ page }) => {
  await page.goto('/review')
  const firstCandidate = page.locator('button.candidate-body').first()
  await expect(firstCandidate).toBeVisible({ timeout: 20_000 })
  await firstCandidate.click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible({ timeout: 20_000 })
  await settle(page)
  await expect(dialog).toHaveScreenshot('candidate-dialog.png')
})
