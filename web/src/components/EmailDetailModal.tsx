import React, { useEffect, useState } from 'react';
import {
  AlertBanner,
  Button,
  KeyValueList,
  LoadingState,
  Modal,
} from '@yggdrasil-lab/ui';
import { Mail } from 'lucide-react';
import type { EmailActionKind } from '../hooks/useIrisEmails';
import {
  ROUTE_TARGET_OPTIONS,
  type InboundEmailDetailResponse,
  type RouteTargetType,
} from '../types/email';
import {
  buildEmailMetadataItems,
  EmailAttachmentsSection,
  EmailOperatorControls,
  EmailPayloadsSection,
} from './EmailDetailSections';

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

  const isBusy = actionLoading !== null;
  const metadataItems = email ? buildEmailMetadataItems(email) : [];

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      disableClose={isBusy}
      title={email ? email.subject || '(No Subject)' : 'Inbound Email Inspector'}
      subtitle={email ? `Email #${email.id} | ${email.from_address}` : undefined}
      headerIcon={<Mail className="w-4 h-4 text-sky-400" />}
      className="sm:max-w-2xl"
      footer={
        <div className="flex justify-end">
          <Button
            variant="secondary"
            size="sm"
            disabled={isBusy}
            onClick={onClose}
            className="w-full sm:w-auto min-h-[44px]"
            data-testid="close-detail-modal-button"
          >
            Close
          </Button>
        </div>
      }
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

          <EmailOperatorControls
            email={email}
            selectedRoute={selectedRoute}
            confirmIgnoreOpen={confirmIgnoreOpen}
            actionLoading={actionLoading}
            onSelectRoute={setSelectedRoute}
            onToggleConfirmIgnore={() => setConfirmIgnoreOpen((prev) => !prev)}
            onCloseConfirmIgnore={() => setConfirmIgnoreOpen(false)}
            onRetry={onRetry}
            onReroute={onReroute}
            onIgnore={onIgnore}
          />

          <EmailAttachmentsSection attachments={email.attachments} />
          <EmailPayloadsSection email={email} />
        </div>
      )}
    </Modal>
  );
};
