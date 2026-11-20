import React from 'react';
import { GlassCard, StatusBadge } from '@yggdrasil-lab/ui';
import { AlertCircle, Paperclip, Sparkles } from 'lucide-react';
import { ROUTE_TARGET_LABELS, type InboundEmailRead } from '../types/email';
import { formatConfidence, formatTimestamp, getStatusPresentation } from '../utils/formatters';

export interface EmailListRowProps {
  email: InboundEmailRead;
  onSelect: (id: number) => void;
}

export const EmailListRow: React.FC<EmailListRowProps> = ({ email, onSelect }) => {
  const statusMeta = getStatusPresentation(email.dispatch_status);
  const routeLabel = ROUTE_TARGET_LABELS[email.route_target] || email.route_target;
  const attachmentCount = email.attachments_meta?.length || 0;
  const confidenceText = formatConfidence(email.jev_verdict?.confidence);

  return (
    <GlassCard
      hoverGlow
      onClick={() => onSelect(email.id)}
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

        <div className="flex flex-wrap items-center gap-1.5 min-w-0">
          <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-sky-500/15 text-sky-300 border border-sky-500/30">
            {routeLabel}
          </span>

          {email.jev_verdict !== null && (
            <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-slate-800/80 text-slate-300 border border-slate-700/60 tabular-nums">
              <Sparkles className="w-3 h-3 text-amber-400 shrink-0" />
              <span>Conf {confidenceText}</span>
            </span>
          )}

          {attachmentCount > 0 && (
            <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-medium bg-slate-800/80 text-slate-300 border border-slate-700/60 tabular-nums">
              <Paperclip className="w-3 h-3 text-slate-400 shrink-0" />
              <span>
                {attachmentCount} {attachmentCount === 1 ? 'file' : 'files'}
              </span>
            </span>
          )}

          {Boolean(email.consumer_ref) && (
            <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-[11px] font-mono bg-emerald-500/10 text-emerald-300 border border-emerald-500/25 max-w-[200px] min-w-0">
              <span className="truncate">Ref: {email.consumer_ref}</span>
            </span>
          )}

          {email.retry_count > 0 && (
            <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[11px] font-medium bg-slate-800/70 text-slate-400 border border-slate-700/50 tabular-nums">
              Retries: {email.retry_count}
            </span>
          )}
        </div>

        {Boolean(email.raw_excerpt) && (
          <p className="text-xs text-slate-400 leading-relaxed line-clamp-2 break-words m-0">
            {email.raw_excerpt}
          </p>
        )}

        {Boolean(email.error_detail) && (
          <div className="flex items-start gap-1.5 px-2.5 py-1.5 rounded-xl bg-rose-500/10 border border-rose-500/25 text-xs text-rose-300 min-w-0">
            <AlertCircle className="w-3.5 h-3.5 text-rose-400 shrink-0 mt-0.5" />
            <span className="break-all line-clamp-2">{email.error_detail}</span>
          </div>
        )}
      </div>
    </GlassCard>
  );
};
