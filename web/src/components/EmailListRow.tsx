import React from 'react';
import { AlertBanner, GlassCard, StatusBadge } from '@yggdrasil-lab/ui';
import { AlertCircle, Paperclip, Sparkles } from 'lucide-react';
import { ROUTE_TARGET_LABELS, type InboundEmailRead } from '../types/email';
import { formatConfidence, formatTimestamp, getStatusPresentation } from '../utils/formatters';

export interface EmailListRowProps {
  email: InboundEmailRead;
  onSelect: (id: number) => void;
}

interface EmailListBadgesProps {
  email: InboundEmailRead;
}

const EmailListBadges: React.FC<EmailListBadgesProps> = ({ email }) => {
  const routeLabel = ROUTE_TARGET_LABELS[email.route_target] || email.route_target;
  const attachmentCount = email.attachments_meta?.length || 0;
  const confidenceText = formatConfidence(email.jev_verdict?.confidence);

  return (
    <div className="flex flex-wrap items-center gap-1.5 min-w-0">
      <StatusBadge label={routeLabel} variant="info" size="xs" showDot={false} />

      {email.jev_verdict !== null && (
        <StatusBadge
          label={`Conf ${confidenceText}`}
          variant="idle"
          size="xs"
          showDot={false}
          icon={<Sparkles className="w-3 h-3 text-amber-400" />}
          className="tabular-nums"
        />
      )}

      {attachmentCount > 0 && (
        <StatusBadge
          label={`${attachmentCount} ${attachmentCount === 1 ? 'file' : 'files'}`}
          variant="idle"
          size="xs"
          showDot={false}
          icon={<Paperclip className="w-3 h-3 text-slate-400" />}
          className="tabular-nums"
        />
      )}

      {Boolean(email.consumer_ref) && (
        <StatusBadge
          label={`Ref: ${email.consumer_ref}`}
          variant="success"
          size="xs"
          showDot={false}
          className="font-mono max-w-[200px]"
        />
      )}

      {email.retry_count > 0 && (
        <StatusBadge
          label={`Retries: ${email.retry_count}`}
          variant="idle"
          size="xs"
          showDot={false}
          className="tabular-nums text-slate-400"
        />
      )}
    </div>
  );
};

export const EmailListRow: React.FC<EmailListRowProps> = ({ email, onSelect }) => {
  const statusMeta = getStatusPresentation(email.dispatch_status);

  return (
    <GlassCard
      hoverGlow
      onClick={() => onSelect(email.id)}
      data-testid={`email-row-${email.id}`}
      className="p-4 sm:p-5 overflow-hidden hover:border-sky-500/40"
    >
      <div className="space-y-2.5 min-w-0">
        <div className="flex items-start justify-between gap-3 min-w-0">
          <div className="min-w-0 flex-1 space-y-1">
            <h3 className="text-sm sm:text-base font-semibold text-slate-100 leading-snug break-words line-clamp-2 m-0">
              {email.subject || '(No Subject)'}
            </h3>
            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-slate-400 min-w-0">
              <span className="text-slate-300 font-medium truncate max-w-full">
                {email.from_address}
              </span>
              <span className="text-slate-600 hidden sm:inline">|</span>
              <span className="whitespace-nowrap tabular-nums">
                {formatTimestamp(email.received_at)}
              </span>
            </div>
          </div>

          <StatusBadge
            label={statusMeta.label}
            variant={statusMeta.variant}
            className="shrink-0"
          />
        </div>

        <EmailListBadges email={email} />

        {Boolean(email.raw_excerpt) && (
          <p className="text-xs text-slate-400 leading-relaxed line-clamp-2 break-words m-0">
            {email.raw_excerpt}
          </p>
        )}

        {Boolean(email.error_detail) && (
          <AlertBanner
            variant="error"
            icon={<AlertCircle className="w-3.5 h-3.5 text-rose-400" />}
            className="px-2.5 py-1.5"
          >
            <span className="break-all line-clamp-2">{email.error_detail}</span>
          </AlertBanner>
        )}
      </div>
    </GlassCard>
  );
};
