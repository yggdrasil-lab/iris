import { devices, expect, test, type Locator, type Page } from '@playwright/test';
import { setupMockApi } from './helpers/mock-data';

const PLATFORMS = [
  {
    name: 'desktop',
    options: {
      viewport: { width: 1280, height: 800 },
      isMobile: false,
      hasTouch: false,
    },
  },
  {
    name: 'ios',
    options: {
      viewport: devices['iPhone 14'].viewport,
      userAgent: devices['iPhone 14'].userAgent,
      deviceScaleFactor: devices['iPhone 14'].deviceScaleFactor,
      isMobile: devices['iPhone 14'].isMobile,
      hasTouch: devices['iPhone 14'].hasTouch,
    },
  },
  {
    name: 'android',
    options: {
      viewport: devices['Pixel 7'].viewport,
      userAgent: devices['Pixel 7'].userAgent,
      deviceScaleFactor: devices['Pixel 7'].deviceScaleFactor,
      isMobile: devices['Pixel 7'].isMobile,
      hasTouch: devices['Pixel 7'].hasTouch,
    },
  },
];

async function assertNoHorizontalOverflow(page: Page): Promise<void> {
  const hasOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth + 1,
  );
  expect(hasOverflow).toBe(false);
}

async function assertMinTouchTarget(locator: Locator): Promise<void> {
  await expect(locator).toBeVisible();
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.height).toBeGreaterThanOrEqual(43.5);
}

async function assertDashboardTouchTargets(page: Page): Promise<void> {
  const targets = [
    page.getByTestId('kpi-tile-total'),
    page.getByTestId('kpi-tile-needs_review'),
    page.getByTestId('email-search-input'),
    page.getByTestId('route-target-filter'),
    page.getByRole('button', { name: 'Needs Review', exact: true }),
  ];
  for (const target of targets) {
    await assertMinTouchTarget(target);
  }
}

async function assertModalTouchTargets(page: Page): Promise<void> {
  const targets = [
    page.getByTestId('retry-email-button'),
    page.getByTestId('reclassify-retry-button'),
    page.getByTestId('ignore-email-button'),
    page.getByTestId('reroute-target-select'),
    page.getByTestId('apply-reroute-button'),
    page.getByTestId('close-detail-modal-button'),
  ];
  for (const target of targets) {
    await assertMinTouchTarget(target);
  }
}

for (const { name: platform, options } of PLATFORMS) {
  test.describe(`Visual Inspection - ${platform.toUpperCase()}`, () => {
    test.use({ ...options });

    test(`captures baseline views and enforces layout invariants on ${platform}`, async ({ page }) => {
      const consoleErrors: string[] = [];
      const pageErrors: string[] = [];
      page.on('console', (msg) => {
        if (msg.type() === 'error') consoleErrors.push(msg.text());
      });
      page.on('pageerror', (err) => pageErrors.push(err.message));

      await setupMockApi(page);
      await page.goto('/');

      await expect(page.getByRole('heading', { name: 'Iris Gateway' })).toBeVisible();
      await expect(page.getByTestId('email-row-101')).toBeVisible();
      await assertNoHorizontalOverflow(page);
      await assertDashboardTouchTargets(page);

      await page.screenshot({
        path: `screenshots/${platform}/01-dashboard-overview.png`,
        fullPage: platform === 'desktop',
      });

      // Active KPI filter state
      await page.getByTestId('kpi-tile-needs_review').click();
      await expect(page.getByTestId('kpi-tile-needs_review')).toHaveAttribute('aria-pressed', 'true');
      await expect(page.getByTestId('email-row-102')).toBeVisible();
      await assertNoHorizontalOverflow(page);

      await page.screenshot({
        path: `screenshots/${platform}/02-kpi-filter-active.png`,
        fullPage: platform === 'desktop',
      });

      // Detail modal inspection
      await page.getByTestId('email-row-102').click();
      await expect(
        page.getByRole('heading', { level: 2, name: 'Invoice #2048 - Northwind Hardware Supply' }),
      ).toBeVisible();
      await expect(page.getByTestId('retry-email-button')).toBeVisible();
      await assertNoHorizontalOverflow(page);
      await assertModalTouchTargets(page);

      await page.screenshot({
        path: `screenshots/${platform}/03-email-detail-modal.png`,
      });

      expect(consoleErrors).toEqual([]);
      expect(pageErrors).toEqual([]);
    });
  });
}
