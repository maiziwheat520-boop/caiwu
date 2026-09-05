import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { expect, test } from '@playwright/test'

const surfaces = ['overview', 'payroll', 'personal-finance', 'reconciliation', 'company-reports']

for (const surface of surfaces) {
  for (const width of [null, 390, 1920]) {
  test(`interface audit ${surface} ${width ?? 'default'}`, async ({ page, baseURL }, testInfo) => {
    if (width) await page.setViewportSize({ width, height: width === 390 ? 844 : 1080 })
    expect(baseURL).toBe('http://127.0.0.1:4173')
    const session = await page.request.get('/api/v1/session')
    expect((await session.json()).runtime_mode).toBe('synthetic-preview')
    await page.goto(`/${surface}`)
    await expect(page.locator('h1').first()).toBeVisible()
    await page.waitForLoadState('networkidle')
    await page.evaluate(() => document.fonts.ready)
    const directory = process.env.UI_AUDIT_DIR ?? testInfo.outputDir
    mkdirSync(directory, { recursive: true })
    await page.screenshot({ path: join(directory, `${surface}-${testInfo.project.name}${width ? `-${width}` : ''}.png`), fullPage: true })
    if (surface === 'overview' && !width) {
      await page.screenshot({ path: join(directory, `overview-${testInfo.project.name}-viewport.png`) })
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(page.viewportSize()!.width)
  })
  }
}
