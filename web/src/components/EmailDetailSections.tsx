import React from 'react';
import {
  Button,
  ConfirmActionBox,
  GlassCard,
  PayloadViewer,
  SectionHeader,
  SelectInput,
  StatusBadge,
  type KeyValueItem,
} from '@yggdrasil-lab/ui';
import {
  Ban,
  Download,
  Paperclip,
  RefreshCw,
  Route,
  SlidersHorizontal,
  Sparkles,
} from 'lucide-react';
import type { EmailActionKind } from '../hooks/useIrisEmails';
import {
  ROUTE_TARGET_LABELS,
  ROUTE_TARGET_OPTIONS,
  type InboundEmailDetailResponse,
  type RouteTargetType,
} from '../types/email';
import {
  downloadBase64Attachment,
  formatBytes,
  formatConfidence,
  formatTimestamp,
  getStatusPresentation,
} from '../utils/formatters';

export function buildEmailMetadataItems(
  email: InboundEmailDetailResponse,
): KeyValueItem[] {
  const statusMeta = getStatusPresentation(email.dispatch_status);
  const items: KeyValueItem[] = [
    { id: 'from', label: 'From', value: email.from_address, copyValue: email.from_address },
    { id: 'to', label: 'To', value: email.to_address },
    { id: 'received', label: 'Received', value: formatTimestamp(email.received_at) },
    {
      id: 'message_id',
      label: 'Message-ID',
      value: email.message_id,
      copyValue: email.message_id,
      mono: true,
    },
    {
      id: 'route_target',
      label: 'Route Target',
      value: ROUTE_TARGET_LABELS[email.route_target] || email.route_target,
    },
    {
      id: 'status',
      label: 'Status',
      value: <StatusBadge label={statusMeta.label} variant={statusMeta.variant} />,
    },
    {
      id: 'confidence',
      label: 'Jev Confidence',
      value: formatConfidence(email.jev_verdict?.confidence),
    },
    { id: 'retry_count', label: 'Retry Count', value: String(email.retry_count) },
    {
      id: 'consumer_ref',
      label: 'Consumer Ref',
      value: email.consumer_ref || null,
      copyValue: email.consumer_ref || undefined,
      mono: Boolean(email.consumer_ref),
    },
  ];

  if (email.error_detail) {
    items.push({
      id: 'error_detail',
      label: 'Error Detail',
      value: <span className="text-rose-300">{email.error_detail}</span>,
    });
  }
  return items;
}

interface EmailOperatorControlsProps {
  email: InboundEmailDetailResponse;
  selectedRoute: RouteTargetType;
  confirmIgnoreOpen: boolean;
  actionLoading: EmailActionKind;
  onSelectRoute: (route: RouteTargetType) => void;
  onToggleConfirmIgnore: () => void;
  onCloseConfirmIgnore: () => void;
  onRetry: (id: number, reclassify: boolean) => Promise<void>;
  onReroute: (id: number, routeTarget: RouteTargetType) => Promise<void>;
  onIgnore: (id: number) => Promise<void>;
}

export const EmailOperatorControls: React.FC<EmailOperatorControlsProps> = ({
  email,
  selectedRoute,
  confirmIgnoreOpen,
  actionLoading,
  onSelectRoute,
  onToggleConfirmIgnore,
  onCloseConfirmIgnore,
  onRetry,
  onReroute,
  onIgnore,
}) => {
  const isBusy = actionLoading !== null;

  return (
    <GlassCard
      tone="solid"
      padding="md"
      className="bg-background/60 border-border/80 shadow-none space-y-3"
    >
      <SectionHeader
        title="Operator Actions"
        icon={<SlidersHorizontal className="w-3.5 h-3.5" />}
        variant="blue"
        size="sm"
        uppercaseTitle
      />

      <div className="flex flex-wrap gap-2">
        <Button
          variant="primary"
          size="sm"
          icon={<RefreshCw className="w-3.5 h-3.5" />}
          loading={actionLoading === 'retry'}
          loadingText="Retrying..."
          disabled={isBusy}
          onClick={() => void onRetry(email.id, false)}
          className="min-h-[44px] flex-1 sm:flex-none"
          data-testid="retry-email-button"
        >
          Retry
        </Button>

        <Button
          variant="blue"
          size="sm"
          icon={<Sparkles className="w-3.5 h-3.5" />}
          loading={actionLoading === 'reclassify'}
          loadingText="Reclassifying..."
          disabled={isBusy}
          onClick={() => void onRetry(email.id, true)}
          className="min-h-[44px] flex-1 sm:flex-none"
          data-testid="reclassify-retry-button"
        >
          Reclassify &amp; Retry
        </Button>

        <Button
          variant="danger"
          size="sm"
          icon={<Ban className="w-3.5 h-3.5" />}
          disabled={isBusy || email.dispatch_status === 'ignored'}
          onClick={onToggleConfirmIgnore}
          className="min-h-[44px]"
          data-testid="ignore-email-button"
        >
          Mark Ignored
        </Button>
      </div>

      {confirmIgnoreOpen && (
        <ConfirmActionBox
          title="Mark this email as ignored?"
          description="This sets dispatch_status to ignored without forwarding to any downstream consumer."
          confirmLabel="Mark Ignored"
          cancelLabel="Cancel"
          variant="danger"
          loading={actionLoading === 'ignore'}
          onConfirm={() => {
            void onIgnore(email.id).then(onCloseConfirmIgnore);
          }}
          onCancel={onCloseConfirmIgnore}
        />
      )}

      <div className="pt-2 border-t border-border/60 flex flex-col sm:flex-row gap-2">
        <SelectInput
          id="reroute-target-select"
          data-testid="reroute-target-select"
          aria-label="Override route target"
          variant="blue"
          value={selectedRoute}
          disabled={isBusy}
          onChange={(e) => onSelectRoute(e.target.value as RouteTargetType)}
          options={ROUTE_TARGET_OPTIONS}
          containerClassName="flex-1 min-w-0"
        />

        <Button
          variant="secondary"
          size="sm"
          icon={<Route className="w-3.5 h-3.5" />}
          loading={actionLoading === 'reroute'}
          loadingText="Applying..."
          disabled={isBusy}
          onClick={() => void onReroute(email.id, selectedRoute)}
          className="min-h-[44px] shrink-0"
          data-testid="apply-reroute-button"
        >
          Apply Re-route
        </Button>
      </div>
    </GlassCard>
  );
};

export const EmailAttachmentsSection: React.FC<{
  attachments: InboundEmailDetailResponse['attachments'];
}> = ({ attachments }) => {
  if (attachments.length === 0) return null;

  return (
    <GlassCard
      tone="solid"
      padding="md"
      className="bg-background/60 border-border/80 shadow-none space-y-2.5"
    >
      <SectionHeader
        title={`Stored Attachments (${attachments.length})`}
        icon={<Paperclip className="w-3.5 h-3.5" />}
        variant="slate"
        size="sm"
        uppercaseTitle
      />
      <div className="space-y-2">
        {attachments.map((att) => (
          <div
            key={att.id}
            className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 rounded-xl bg-card/80 border border-border/70 px-3 py-2.5 min-w-0"
          >
            <div className="min-w-0 flex-1">
              <div className="text-xs font-semibold text-slate-100 truncate">
                {att.filename}
              </div>
              <div className="text-[11px] text-slate-400 font-mono truncate mt-0.5">
                {att.content_type} | {formatBytes(att.size_bytes)} | sha256:{att.sha256.slice(0, 12)}
              </div>
            </div>
            <Button
              variant="outline"
              size="sm"
              icon={<Download className="w-3.5 h-3.5" />}
              onClick={() => downloadBase64Attachment(att)}
              className="min-h-[44px] shrink-0"
            >
              Download
            </Button>
          </div>
        ))}
      </div>
    </GlassCard>
  );
};

export const EmailPayloadsSection: React.FC<{
  email: InboundEmailDetailResponse;
}> = ({ email }) => (
  <div className="space-y-2.5 min-w-0">
    <PayloadViewer
      title="Jev Routing Verdict"
      subtitle={
        email.jev_verdict
          ? `Provider: ${String(email.jev_verdict.provider || 'muninn-jev')}`
          : undefined
      }
      payload={email.jev_verdict}
      defaultExpanded={true}
      emptyMessage="No Jev routing verdict recorded"
    />

    <PayloadViewer
      title="Plain Text Body"
      payload={email.text_body || email.raw_excerpt}
      defaultExpanded={true}
      emptyMessage="No plain text body recorded"
    />

    <PayloadViewer
      title="Sanitized HTML Excerpt"
      payload={email.html_excerpt}
      defaultExpanded={false}
      emptyMessage="No HTML excerpt recorded"
    />
  </div>
);
