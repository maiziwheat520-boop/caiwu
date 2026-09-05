import { expect, test, type Locator, type Page } from '@playwright/test'

async function settle(page: Page) {
  await page.addStyleTag({
    content: `*, *::before, *::after {
      animation-duration: 0s !important;
      transition-duration: 0s !important;
      caret-color: transparent !important;
    }`,
  })
  await page.evaluate(() => document.fonts?.ready)
}

async function snapshot(locator: Locator, name: string) {
  await expect(locator).toBeVisible()
  await expect(locator).toHaveScreenshot(name)
}

test('company report range applies as one period', async ({ page }) => {
  await page.goto('/company-reports')
  await expect(page.getByRole('heading', { name: '公司报表', exact: true })).toBeVisible()
  await settle(page)

  const start = page.getByLabel('开始月份')
  const apply = page.getByRole('button', { name: '应用期间' })
  await expect(apply).toBeDisabled()
  await start.fill('2026-02')
  await expect(apply).toBeEnabled()
  await apply.click()
  await expect(start).toHaveValue('2026-02')
  await expect(apply).toBeDisabled()

  await snapshot(page.locator('.company-report-range'), 'company-report-period-range.png')
})

test('monthly reconciliation exposes an immediate labelled month', async ({ page }) => {
  await page.goto('/reconciliation')
  await expect(page.getByRole('heading', { name: '月度对账', exact: true })).toBeVisible()
  await settle(page)

  const month = page.getByLabel('选择对账月份')
  await expect(page.getByText('对账月份', { exact: true })).toBeVisible()
  await month.fill('2026-08')
  await expect(month).toHaveValue('2026-08')

  await snapshot(page.locator('.original-reconciliation-filters'), 'reconciliation-month-filter.png')
})

test('candidate review reuses the labelled month field', async ({ page }) => {
  await page.goto('/review')
  const firstCandidate = page.locator('button.candidate-body').first()
  await expect(firstCandidate).toBeVisible()
  await firstCandidate.click()
  await settle(page)

  const month = page.getByLabel('归属月份')
  await expect(month).toHaveAttribute('type', 'month')
  await snapshot(month.locator('..'), 'candidate-accounting-month.png')
})
