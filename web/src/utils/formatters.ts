import type { StatusBadgeVariant } from '@yggdrasil-lab/ui';
import type { InboundEmailAttachmentRead } from '../types/email';

export interface StatusPresentation {
  label: string;
  variant: StatusBadgeVariant;
}

const STATUS_MAP: Record<string, StatusPresentation> = {
  dispatched: { label: 'Dispatched', variant: 'success' },
  needs_review: { label: 'Needs Review', variant: 'warning' },
  failed: { label: 'Failed', variant: 'error' },
  ignored: { label: 'Ignored', variant: 'idle' },
  received: { label: 'Received', variant: 'info' },
  duplicate: { label: 'Duplicate', variant: 'idle' },
};

export function getStatusPresentation(status: string): StatusPresentation {
  const key = status.trim().toLowerCase();
  return STATUS_MAP[key] || { label: status || 'Unknown', variant: 'idle' };
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatTimestamp(iso: string): string {
  if (!iso) return '-';
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return iso;
  return new Intl.DateTimeFormat('en-NZ', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed);
}

export function formatConfidence(confidence: number | undefined | null): string {
  if (typeof confidence !== 'number' || !Number.isFinite(confidence)) {
    return '-';
  }
  return `${Math.round(confidence * 100)}%`;
}

export function downloadBase64Attachment(attachment: InboundEmailAttachmentRead): void {
  const binary = window.atob(attachment.content_base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  const blob = new Blob([bytes], {
    type: attachment.content_type || 'application/octet-stream',
  });
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = objectUrl;
  anchor.download = attachment.filename || 'attachment.bin';
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(objectUrl);
}
