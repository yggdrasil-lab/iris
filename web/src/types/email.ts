export type RouteTargetType =
  | 'plutus_dbs_alert'
  | 'plutus_purchase_receipt'
  | 'plutus_payslip'
  | 'plutus_bank_statement'
  | 'travel_booking'
  | 'actionable_notice'
  | 'ignore';

export type DispatchStatusType =
  | 'received'
  | 'dispatched'
  | 'ignored'
  | 'needs_review'
  | 'failed'
  | 'duplicate';

export interface JevVerdict {
  route_target: RouteTargetType | string;
  has_actionable_amount: boolean;
  has_schedule_event?: boolean;
  schedule_confidence?: number | null;
  has_travel_booking?: boolean;
  travel_confidence?: number | null;
  confidence: number;
  urgency_score: number;
  reason: string;
  provider: string;
  [key: string]: unknown;
}

export interface InboundEmailDeliveryRead {
  id: number;
  email_id: number;
  consumer: string;
  dispatch_status: string;
  consumer_ref: string | null;
  error_detail: string | null;
  retry_count: number;
  dispatched_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface InboundEmailAttachmentMeta {
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
}

export interface InboundEmailAttachmentRead {
  id: number;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  content_base64: string;
  created_at: string;
}

export interface InboundEmailRead {
  id: number;
  message_id: string;
  from_address: string;
  to_address: string;
  subject: string;
  received_at: string;
  raw_excerpt: string | null;
  attachments_meta: InboundEmailAttachmentMeta[];
  jev_verdict: JevVerdict | null;
  route_target: string;
  dispatch_status: string;
  consumer_ref: string | null;
  error_detail: string | null;
  retry_count: number;
  deliveries?: InboundEmailDeliveryRead[];
  created_at: string;
  updated_at: string;
}

export interface InboundEmailDetailResponse extends InboundEmailRead {
  text_body: string | null;
  html_excerpt: string | null;
  attachments: InboundEmailAttachmentRead[];
}

export interface InboundEmailListResponse {
  items: InboundEmailRead[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface InboundEmailStatsResponse {
  total: number;
  by_status: Record<string, number>;
  by_route_target: Record<string, number>;
}

export const ROUTE_TARGET_OPTIONS: Array<{ value: RouteTargetType; label: string }> = [
  { value: 'plutus_dbs_alert', label: 'DBS Alert' },
  { value: 'plutus_purchase_receipt', label: 'Purchase Receipt' },
  { value: 'plutus_payslip', label: 'Payslip' },
  { value: 'plutus_bank_statement', label: 'Bank Statement' },
  { value: 'travel_booking', label: 'Travel Booking (Odysseus)' },
  { value: 'actionable_notice', label: 'Actionable Notice' },
  { value: 'ignore', label: 'Ignore' },
];

export const ROUTE_TARGET_LABELS: Record<string, string> = {
  plutus_dbs_alert: 'DBS Alert',
  plutus_purchase_receipt: 'Purchase Receipt',
  plutus_payslip: 'Payslip',
  plutus_bank_statement: 'Bank Statement',
  travel_booking: 'Travel Booking (Odysseus)',
  actionable_notice: 'Actionable Notice',
  ignore: 'Ignore',
  unclassified: 'Unclassified',
};
