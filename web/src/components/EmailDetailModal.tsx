import React, { useEffect, useState } from 'react';
import {
  AlertBanner,
  Button,
  ConfirmActionBox,
  KeyValueList,
  LoadingState,
  Modal,
  PayloadViewer,
  StatusBadge,
  type KeyValueItem,
} from '@yggdrasil-lab/ui';
import { Ban, Download, Mail, Paperclip, RefreshCw, Route, Sparkles } from 'lucide-react';
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

export interface EmailDetailModalProps {
  isOpen: boolean;
  onClose: () => void;
  email: InboundEmailDetailResponse | null;
  isLoading: boolean;
  actionLoading: EmailActionKind;
  actionError: string | null;
  onRetry: (id: number, reclassify: boolean) => Promise<void>;
  onReroute: (id: number, routeTarget: RouteTargetType) => Promise<void>;
  onIgnore: (id: number) => Promise<void>;
}

function resolveInitialRouteTarget(rawTarget: string | undefined): RouteTargetType {
  const match = ROUTE_TARGET_OPTIONS.find((opt) => opt.value === rawTarget);
  return match ? match.value : 'plutus_dbs_alert';
}

export const EmailDetailModal: React.FC<EmailDetailModalProps> = ({
  isOpen,
  onClose,
  email,
  isLoading,
  actionLoading,
  actionError,
  onRetry,
  onReroute,
  onIgnore,
}) => {
  const [selectedRoute, setSelectedRoute] = useState<RouteTargetType>('plutus_dbs_alert');
  const [confirmIgnoreOpen, setConfirmIgnoreOpen] = useState<boolean>(false);

  useEffect(() => {
    if (email) {
      setSelectedRoute(resolveInitialRouteTarget(email.route_target));
      setConfirmIgnoreOpen(false);
    }
  }, [email]);

  const statusMeta = email ? getStatusPresentation(email.dispatch_status) : null;
  const isBusy = actionLoading !== null;

  const metadataItems: KeyValueItem[] = email
    ? [
        {
          id: 'from',
          label: 'From',
          value: email.from_address,
          copyValue: email.from_address,
        },
        {
          id: 'to',
          label: 'To',
          value: email.to_address,
        },
        {
          id: 'received',
          label: 'Received',
          value: formatTimestamp(email.received_at),
        },
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
          value: statusMeta ? (
            <StatusBadge label={statusMeta.label} variant={statusMeta.variant} />
          ) : (
            email.dispatch_status
          ),
        },
        {
          id: 'confidence',
          label: 'Jev Confidence',
          value: formatConfidence(email.jev_verdict?.confidence),
        },
        {
          id: 'retry_count',
          label: 'Retry Count',
          value: String(email.retry_count),
        },
        {
          id: 'consumer_ref',
          label: 'Consumer Ref',
          value: email.consumer_ref || null,
          copyValue: email.consumer_ref || undefined,
          mono: Boolean(email.consumer_ref),
        },
        ...(Boolean(email.error_detail)
          ? [
              {
                id: 'error_detail',
                label: 'Error Detail',
                value: <span className="text-rose-300">{email.error_detail}</span>,
              },
            ]
          : []),
      ]
    : [];

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      disableClose={isBusy}
      title={email ? email.subject || '(No Subject)' : 'Inbound Email Inspector'}
      subtitle={email ? `Email #${email.id} | ${email.from_address}` : undefined}
      headerIcon={<Mail className="w-4 h-4 text-sky-400" />}
      className="sm:max-w-2xl"
    >
      {isLoading && <LoadingState label="Loading email payload..." variant="blue" />}

      {!isLoading && Boolean(actionError) && (
        <AlertBanner variant="error" title="Operator Action Error">
          {actionError}
        </AlertBanner>
      )}

      {!isLoading && email && (
        <div className="space-y-4 min-w-0">
          <KeyValueList items={metadataItems} columns={2} />

          {/* Operator Controls */}
          <div className="rounded-2xl bg-background/60 border border-border/80 p-3.5 space-y-3">
            <div className="text-[11px] uppercase tracking-wider text-slate-400 font-semibold">
              Operator Actions
            </div>

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
                Reclassify & Retry
              </Button>

              <Button
                variant="danger"
                size="sm"
                icon={<Ban className="w-3.5 h-3.5" />}
                disabled={isBusy || email.dispatch_status === 'ignored'}
                onClick={() => setConfirmIgnoreOpen((prev) => !prev)}
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
                  void onIgnore(email.id).then(() => setConfirmIgnoreOpen(false));
                }}
                onCancel={() => setConfirmIgnoreOpen(false)}
              />
            )}

            <div className="pt-1 border-t border-border/60 flex flex-col sm:flex-row gap-2">
              <div className="flex-1 min-w-0">
                <label htmlFor="reroute-target-select" className="sr-only">
                  Override route target
                </label>
                <select
                  id="reroute-target-select"
                  data-testid="reroute-target-select"
                  value={selectedRoute}
                  disabled={isBusy}
                  onChange={(e) => setSelectedRoute(e.target.value as RouteTargetType)}
                  className="w-full min-h-[44px] px-3 py-2 bg-card border border-border rounded-xl text-base sm:text-xs text-slate-100 focus:outline-none focus:border-blue-500 transition-colors"
                >
                  {ROUTE_TARGET_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
              </div>

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
          </div>

          {/* Stored Attachments */}
          {email.attachments.length > 0 && (
            <div className="rounded-2xl bg-background/60 border border-border/80 p-3.5 space-y-2.5">
              <div className="text-[11px] uppercase tracking-wider text-slate-400 font-semibold flex items-center gap-1.5">
                <Paperclip className="w-3.5 h-3.5" />
                <span>Stored Attachments ({email.attachments.length})</span>
              </div>
              <div className="space-y-2">
                {email.attachments.map((att) => (
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
            </div>
          )}

          {/* Payloads */}
          <div className="space-y-2.5 min-w-0">
            <PayloadViewer
              title="Jev Routing Verdict"
              subtitle={email.jev_verdict ? `Provider: ${String(email.jev_verdict.provider || 'muninn-jev')}` : undefined}
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
        </div>
      )}
    </Modal>
  );
};
