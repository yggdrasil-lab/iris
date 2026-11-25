import { expect, test } from '@playwright/test';
import { setupMockApi } from './helpers/mock-data';

test.describe('Iris Audit Dashboard E2E', () => {
  test('renders KPI tiles, email list, and status/route/search filters', async ({ page }) => {
    await setupMockApi(page);
    await page.goto('/');

    await expect(page.getByRole('heading', { name: 'Iris Gateway' })).toBeVisible();
    await expect(page.getByTestId('kpi-tile-total')).toContainText('4');
    await expect(page.getByTestId('kpi-tile-needs_review')).toContainText('1');
    await expect(page.getByTestId('kpi-tile-failed')).toContainText('1');
    await expect(page.getByTestId('kpi-tile-dispatched')).toContainText('1');

    await expect(page.getByTestId('email-row-101')).toBeVisible();
    await expect(page.getByTestId('email-row-102')).toBeVisible();
    await expect(page.getByTestId('email-row-103')).toBeVisible();
    await expect(page.getByTestId('email-row-104')).toBeVisible();

    // Toggle KPI filter tile on and off
    const reviewTile = page.getByTestId('kpi-tile-needs_review');
    await reviewTile.click();
    await expect(reviewTile).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByTestId('email-row-102')).toBeVisible();
    await expect(page.getByTestId('email-row-101')).toBeHidden();

    await reviewTile.click();
    await expect(page.getByTestId('kpi-tile-total')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByTestId('email-row-101')).toBeVisible();

    // Route filter + debounced search
    await page.getByTestId('route-target-filter').selectOption('plutus_payslip');
    await expect(page.getByTestId('email-row-103')).toBeVisible();
    await expect(page.getByTestId('email-row-101')).toBeHidden();

    await page.getByTestId('route-target-filter').selectOption('all');
    const searchReqPromise = page.waitForRequest(
      (req) => req.url().includes('/api/emails?') && req.url().includes('q=Harbour'),
    );
    await page.getByTestId('email-search-input').fill('Harbour');
    await searchReqPromise;
    await expect(page.getByTestId('email-row-101')).toBeVisible();
    await expect(page.getByTestId('email-row-102')).toBeHidden();
  });

  test('inspects email detail modal and executes reroute, retry, and ignore actions', async ({ page }) => {
    await setupMockApi(page);
    await page.goto('/');

    // Open Needs Review email with attachment
    await page.getByTestId('email-row-102').click();
    await expect(
      page.getByRole('heading', { level: 2, name: 'Invoice #2048 - Northwind Hardware Supply' }),
    ).toBeVisible();
    await expect(page.getByText('invoice-2048.pdf')).toBeVisible();
    await expect(page.getByText('Jev Routing Verdict')).toBeVisible();

    // Reroute action
    await page.getByTestId('reroute-target-select').selectOption('actionable_notice');
    await page.getByTestId('apply-reroute-button').click();
    await expect(page.getByTestId('email-row-102')).toContainText('Actionable Notice');

    // Retry action
    await page.getByTestId('retry-email-button').click();
    await expect(page.getByTestId('email-row-102')).toContainText('Dispatched');

    await page.getByTestId('close-detail-modal-button').click();
    await expect(page.getByTestId('close-detail-modal-button')).toBeHidden();

    // Open failed email and mark ignored via ConfirmActionBox
    await page.getByTestId('email-row-103').click();
    await page.getByTestId('ignore-email-button').click();
    await expect(page.getByText('Mark this email as ignored?')).toBeVisible();
    await page.locator('.fixed button:has-text("Mark Ignored")').last().click();
    await expect(page.getByTestId('email-row-103')).toContainText('Ignored');
  });

  test('handles empty state with filter reset and recovers from API error state', async ({ page }) => {
    await setupMockApi(page, { failInitialLoad: true });
    await page.goto('/');

    await expect(page.getByText('Unable to load audit feed')).toBeVisible();
    await page.getByRole('button', { name: 'Retry' }).click();
    await expect(page.getByTestId('email-row-101')).toBeVisible();

    await page.getByTestId('email-search-input').fill('nonexistent-query-term');
    await expect(page.getByText('No matching inbound emails')).toBeVisible();
    await page.getByRole('button', { name: 'Clear Filters' }).click();
    await expect(page.getByTestId('email-row-101')).toBeVisible();
  });
});
