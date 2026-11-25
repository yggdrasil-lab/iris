import type { Page, Route } from '@playwright/test';
import type {
  InboundEmailDetailResponse,
  InboundEmailStatsResponse,
} from '../../src/types/email';

export interface MockApiOptions {
  empty?: boolean;
  failInitialLoad?: boolean;
}

const FIXTURE_EMAILS: InboundEmailDetailResponse[] = [
  {
    id: 101,
    message_id: '<msg-101@example.com>',
    from_address: 'alerts@pacificbank.example',
    to_address: 'iris-gateway@example.net',
    subject: 'Card Purchase Alert: Harbour Fresh Market NZD 42.50',
    received_at: '2026-11-24T22:15:00Z',
    raw_excerpt: 'You spent NZD 42.50 at Harbour Fresh Market using card ending 1001.',
    attachments_meta: [],
    jev_verdict: {
      route_target: 'plutus_dbs_alert',
      has_actionable_amount: true,
      confidence: 0.96,
      urgency_score: 0.4,
      reason: 'Synthetic card purchase alert matching bank alert template.',
      provider: 'muninn-jev',
    },
    route_target: 'plutus_dbs_alert',
    dispatch_status: 'dispatched',
    consumer_ref: 'plutus-tx-9001',
    error_detail: null,
    retry_count: 0,
    created_at: '2026-11-24T22:15:02Z',
    updated_at: '2026-11-24T22:15:05Z',
    text_body: 'Transaction Alert: NZD 42.50 at Harbour Fresh Market on card ending 1001.',
    html_excerpt: '<p>Transaction Alert: NZD 42.50 at Harbour Fresh Market.</p>',
    attachments: [],
  },
  {
    id: 102,
    message_id: '<msg-102@example.com>',
    from_address: 'Alex Tan <alex.tan@example.com>',
    to_address: 'iris-gateway@example.net',
    subject: 'Invoice #2048 - Northwind Hardware Supply',
    received_at: '2026-11-24T21:40:00Z',
    raw_excerpt: 'Please find attached receipt #2048 for NZD 189.00.',
    attachments_meta: [
      {
        filename: 'invoice-2048.pdf',
        content_type: 'application/pdf',
        size_bytes: 24576,
        sha256: 'abc123def4567890abcdef1234567890',
      },
    ],
    jev_verdict: {
      route_target: 'plutus_purchase_receipt',
      has_actionable_amount: true,
      confidence: 0.72,
      urgency_score: 0.55,
      reason: 'Low confidence threshold requires operator confirmation.',
      provider: 'muninn-jev',
    },
    route_target: 'plutus_purchase_receipt',
    dispatch_status: 'needs_review',
    consumer_ref: null,
    error_detail: null,
    retry_count: 0,
    created_at: '2026-11-24T21:40:03Z',
    updated_at: '2026-11-24T21:40:03Z',
    text_body: 'Hi, attached is invoice #2048 from Northwind Hardware Supply for NZD 189.00.',
    html_excerpt: null,
    attachments: [
      {
        id: 1,
        filename: 'invoice-2048.pdf',
        content_type: 'application/pdf',
        size_bytes: 24576,
        sha256: 'abc123def4567890abcdef1234567890',
        content_base64: 'JVBERi0xLjQK',
        created_at: '2026-11-24T21:40:03Z',
      },
    ],
  },
  {
    id: 103,
    message_id: '<msg-103@example.com>',
    from_address: 'payroll@northwind.example',
    to_address: 'iris-gateway@example.net',
    subject: 'Monthly Payslip - November 2026',
    received_at: '2026-11-24T20:10:00Z',
    raw_excerpt: 'Your November 2026 payslip statement is ready.',
    attachments_meta: [],
    jev_verdict: {
      route_target: 'plutus_payslip',
      has_actionable_amount: false,
      confidence: 0.89,
      urgency_score: 0.3,
      reason: 'Payroll delivery failed downstream timeout.',
      provider: 'muninn-jev',
    },
    route_target: 'plutus_payslip',
    dispatch_status: 'failed',
    consumer_ref: null,
    error_detail: 'Downstream consumer returned HTTP 503 Service Unavailable',
    retry_count: 2,
    created_at: '2026-11-24T20:10:01Z',
    updated_at: '2026-11-24T20:12:00Z',
    text_body: 'Your November 2026 payslip statement is enclosed.',
    html_excerpt: null,
    attachments: [],
  },
  {
    id: 104,
    message_id: '<msg-104@example.com>',
    from_address: 'noreply@example.com',
    to_address: 'iris-gateway@example.net',
    subject: 'Weekly Digest Newsletter',
    received_at: '2026-11-24T18:00:00Z',
    raw_excerpt: 'Top stories from this week.',
    attachments_meta: [],
    jev_verdict: {
      route_target: 'ignore',
      has_actionable_amount: false,
      confidence: 0.99,
      urgency_score: 0.05,
      reason: 'Marketing newsletter classified as ignore.',
      provider: 'muninn-jev',
    },
    route_target: 'ignore',
    dispatch_status: 'ignored',
    consumer_ref: null,
    error_detail: null,
    retry_count: 0,
    created_at: '2026-11-24T18:00:01Z',
    updated_at: '2026-11-24T18:00:02Z',
    text_body: 'Top stories from this week.',
    html_excerpt: null,
    attachments: [],
  },
];

function computeStats(items: InboundEmailDetailResponse[]): InboundEmailStatsResponse {
  const byStatus: Record<string, number> = {};
  const byRoute: Record<string, number> = {};
  for (const item of items) {
    byStatus[item.dispatch_status] = (byStatus[item.dispatch_status] ?? 0) + 1;
    byRoute[item.route_target] = (byRoute[item.route_target] ?? 0) + 1;
  }
  return { total: items.length, by_status: byStatus, by_route_target: byRoute };
}

function filterEmails(
  items: InboundEmailDetailResponse[],
  url: URL,
): InboundEmailDetailResponse[] {
  const status = url.searchParams.get('status');
  const routeTarget = url.searchParams.get('route_target');
  const query = (url.searchParams.get('q') || '').trim().toLowerCase();

  return items.filter((item) => {
    if (status && status !== 'all' && item.dispatch_status !== status) return false;
    if (routeTarget && routeTarget !== 'all' && item.route_target !== routeTarget) return false;
    if (!query) return true;
    const haystack = [item.subject, item.from_address, item.message_id, item.raw_excerpt || '']
      .join(' ')
      .toLowerCase();
    return haystack.includes(query);
  });
}

async function handleActionRoute(
  route: Route,
  emails: InboundEmailDetailResponse[],
  id: number,
  action: string,
): Promise<void> {
  const target = emails.find((item) => item.id === id);
  if (!target) {
    await route.fulfill({ status: 404, json: { detail: 'Email not found' } });
    return;
  }

  if (action === 'retry') {
    target.retry_count += 1;
    target.dispatch_status = 'dispatched';
    target.error_detail = null;
    target.consumer_ref = `plutus-retry-${target.id}`;
  } else if (action === 'reroute') {
    const body = (route.request().postDataJSON() || {}) as { route_target?: string };
    if (body.route_target) target.route_target = body.route_target;
  } else if (action === 'ignore') {
    target.dispatch_status = 'ignored';
    target.error_detail = null;
  }

  await route.fulfill({ status: 200, json: target });
}

async function registerListRoutes(
  page: Page,
  emails: InboundEmailDetailResponse[],
  failState: { fail: boolean },
): Promise<void> {
  await page.route('**/api/emails/stats', async (route) => {
    if (failState.fail) {
      await route.fulfill({ status: 500, json: { detail: 'Simulated stats outage' } });
      return;
    }
    await route.fulfill({ status: 200, json: computeStats(emails) });
  });

  await page.route(/\/api\/emails(\?.*)?$/, async (route) => {
    if (failState.fail) {
      failState.fail = false;
      await route.fulfill({ status: 500, json: { detail: 'Simulated audit feed outage' } });
      return;
    }
    const url = new URL(route.request().url());
    const filtered = filterEmails(emails, url);
    const pageNum = Number(url.searchParams.get('page') || '1');
    const pageSize = Number(url.searchParams.get('page_size') || '25');
    const start = (pageNum - 1) * pageSize;
    await route.fulfill({
      status: 200,
      json: {
        items: filtered.slice(start, start + pageSize),
        total: filtered.length,
        page: pageNum,
        page_size: pageSize,
        total_pages: Math.max(1, Math.ceil(filtered.length / pageSize)),
      },
    });
  });
}

async function registerDetailRoutes(
  page: Page,
  emails: InboundEmailDetailResponse[],
): Promise<void> {
  await page.route(/\/api\/emails\/(\d+)(\/(retry|reroute|ignore))?$/, async (route) => {
    const match = route.request().url().match(/\/api\/emails\/(\d+)(?:\/(retry|reroute|ignore))?$/);
    if (!match) {
      await route.fallback();
      return;
    }
    const id = Number(match[1]);
    const action = match[2];
    if (action && route.request().method() === 'POST') {
      await handleActionRoute(route, emails, id, action);
      return;
    }
    const found = emails.find((item) => item.id === id);
    await route.fulfill(
      found ? { status: 200, json: found } : { status: 404, json: { detail: 'Email not found' } },
    );
  });
}

export async function setupMockApi(
  page: Page,
  options: MockApiOptions = {},
): Promise<{ emails: InboundEmailDetailResponse[] }> {
  const emails = options.empty ? [] : structuredClone(FIXTURE_EMAILS);
  const failState = { fail: Boolean(options.failInitialLoad) };

  await page.route('**/api/client-errors', async (route) => {
    await route.fulfill({ status: 200, json: { status: 'ok' } });
  });
  await registerListRoutes(page, emails, failState);
  await registerDetailRoutes(page, emails);

  return { emails };
}
