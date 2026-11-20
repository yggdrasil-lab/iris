import { authFetch } from '@yggdrasil-lab/ui';
import type {
  InboundEmailDetailResponse,
  InboundEmailListResponse,
  InboundEmailStatsResponse,
  RouteTargetType,
} from '../types/email';

export interface FetchEmailsParams {
  status?: string;
  route_target?: string;
  q?: string;
  page?: number;
  page_size?: number;
}

export class ApiRequestError extends Error {
  readonly status: number;
  readonly updatedEmail?: InboundEmailDetailResponse;

  constructor(message: string, status: number, updatedEmail?: InboundEmailDetailResponse) {
    super(message);
    this.name = 'ApiRequestError';
    this.status = status;
    this.updatedEmail = updatedEmail;
  }
}

async function parseErrorDetail(res: Response): Promise<{ message: string; detailEmail?: InboundEmailDetailResponse }> {
  const fallback = `Request failed with HTTP ${res.status}`;
  try {
    const raw = (await res.json()) as Record<string, unknown>;
    if (typeof raw.detail === 'string' && raw.detail.trim()) {
      return { message: raw.detail };
    }
    if (typeof raw.error_detail === 'string' && raw.error_detail.trim() && typeof raw.id === 'number') {
      return {
        message: raw.error_detail,
        detailEmail: raw as unknown as InboundEmailDetailResponse,
      };
    }
    return { message: fallback };
  } catch {
    return { message: fallback };
  }
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await authFetch(url, init);
  if (!res.ok) {
    const { message, detailEmail } = await parseErrorDetail(res);
    throw new ApiRequestError(message, res.status, detailEmail);
  }
  return (await res.json()) as T;
}

export async function fetchEmailStats(): Promise<InboundEmailStatsResponse> {
  return requestJson<InboundEmailStatsResponse>('/api/emails/stats');
}

export async function fetchEmails(params: FetchEmailsParams): Promise<InboundEmailListResponse> {
  const query = new URLSearchParams();
  if (params.status && params.status !== 'all') {
    query.set('status', params.status);
  }
  if (params.route_target && params.route_target !== 'all') {
    query.set('route_target', params.route_target);
  }
  if (params.q && params.q.trim()) {
    query.set('q', params.q.trim());
  }
  if (params.page) {
    query.set('page', String(params.page));
  }
  if (params.page_size) {
    query.set('page_size', String(params.page_size));
  }
  const qs = query.toString();
  return requestJson<InboundEmailListResponse>(qs ? `/api/emails?${qs}` : '/api/emails');
}

export async function fetchEmailDetail(id: number): Promise<InboundEmailDetailResponse> {
  return requestJson<InboundEmailDetailResponse>(`/api/emails/${id}`);
}

export async function retryEmail(id: number, reclassify = false): Promise<InboundEmailDetailResponse> {
  return requestJson<InboundEmailDetailResponse>(`/api/emails/${id}/retry`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reclassify }),
  });
}

export async function rerouteEmail(
  id: number,
  routeTarget: RouteTargetType,
): Promise<InboundEmailDetailResponse> {
  return requestJson<InboundEmailDetailResponse>(`/api/emails/${id}/reroute`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ route_target: routeTarget }),
  });
}

export async function ignoreEmail(id: number): Promise<InboundEmailDetailResponse> {
  return requestJson<InboundEmailDetailResponse>(`/api/emails/${id}/ignore`, {
    method: 'POST',
  });
}
