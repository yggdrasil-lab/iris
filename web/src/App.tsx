import React from 'react';
import {
  AlertBanner,
  AppHeader,
  AppShell,
  Button,
  EmptyState,
  LoadingState,
  PaginationBar,
  SessionExpiredBanner,
  SummaryMetricGrid,
  type SummaryMetricItem,
} from '@yggdrasil-lab/ui';
import { AlertTriangle, CheckCircle2, Clock, Inbox, Mail, RefreshCw } from 'lucide-react';
import { EmailDetailModal } from './components/EmailDetailModal';
import { EmailFiltersBar } from './components/EmailFiltersBar';
import { EmailListRow } from './components/EmailListRow';
import { useIrisEmails } from './hooks/useIrisEmails';

export const App: React.FC = () => {
  const {
    stats,
    emails,
    total,
    page,
    pageSize,
    setPage,
    statusFilter,
    setStatusFilter,
    routeTargetFilter,
    setRouteTargetFilter,
    searchInput,
    setSearchInput,
    isLoading,
    isRefreshing,
    error,
    sessionExpired,
    refreshAll,
    isDetailOpen,
    selectedEmail,
    isDetailLoading,
    actionLoading,
    actionError,
    openEmailDetail,
    closeEmailDetail,
    handleRetry,
    handleReroute,
    handleIgnore,
  } = useIrisEmails();

  const byStatus = stats?.by_status || {};
  const totalCount = stats?.total ?? 0;
  const needsReviewCount = byStatus.needs_review ?? 0;
  const failedCount = byStatus.failed ?? 0;
  const dispatchedCount = byStatus.dispatched ?? 0;

  const summaryItems: SummaryMetricItem[] = [
    {
      id: 'total',
      label: 'Total Captured',
      icon: <Inbox className="w-3 h-3 text-sky-400" />,
      value: (
        <button
          type="button"
          onClick={() => setStatusFilter('all')}
          className={`w-full rounded-lg px-1.5 py-0.5 transition-colors hover:bg-slate-800/70 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/60 cursor-pointer ${
            statusFilter === 'all' ? 'underline decoration-sky-400 underline-offset-4' : ''
          }`}
        >
          {totalCount}
        </button>
      ),
      subvalue: 'All inbound mail',
    },
    {
      id: 'needs_review',
      label: 'Needs Review',
      icon: <Clock className="w-3 h-3 text-amber-400" />,
      valueClassName: 'text-amber-400',
      value: (
        <button
          type="button"
          onClick={() => setStatusFilter('needs_review')}
          className={`w-full rounded-lg px-1.5 py-0.5 transition-colors hover:bg-slate-800/70 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/60 cursor-pointer ${
            statusFilter === 'needs_review' ? 'underline decoration-amber-400 underline-offset-4' : ''
          }`}
        >
          {needsReviewCount}
        </button>
      ),
      subvalue: 'Operator queue',
    },
    {
      id: 'failed',
      label: 'Failed',
      icon: <AlertTriangle className="w-3 h-3 text-rose-400" />,
      valueClassName: 'text-rose-400',
      value: (
        <button
          type="button"
          onClick={() => setStatusFilter('failed')}
          className={`w-full rounded-lg px-1.5 py-0.5 transition-colors hover:bg-slate-800/70 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-500/60 cursor-pointer ${
            statusFilter === 'failed' ? 'underline decoration-rose-400 underline-offset-4' : ''
          }`}
        >
          {failedCount}
        </button>
      ),
      subvalue: 'Needs retry',
    },
    {
      id: 'dispatched',
      label: 'Dispatched',
      icon: <CheckCircle2 className="w-3 h-3 text-emerald-400" />,
      valueClassName: 'text-emerald-400',
      value: (
        <button
          type="button"
          onClick={() => setStatusFilter('dispatched')}
          className={`w-full rounded-lg px-1.5 py-0.5 transition-colors hover:bg-slate-800/70 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/60 cursor-pointer ${
            statusFilter === 'dispatched' ? 'underline decoration-emerald-400 underline-offset-4' : ''
          }`}
        >
          {dispatchedCount}
        </button>
      ),
      subvalue: 'Delivered cleanly',
    },
  ];

  const header = (
    <AppHeader
      variant="blue"
      title="Iris Gateway"
      subtitle="Inbound Email Audit & Routing"
      brandIcon={<Mail className="w-4 h-4" />}
      onRefresh={() => void refreshAll()}
      isRefreshing={isRefreshing}
    />
  );

  return (
    <AppShell header={header}>
      <div className="space-y-4 min-w-0">
        {sessionExpired && <SessionExpiredBanner />}

        {Boolean(error) && (
          <AlertBanner
            variant="error"
            title="Unable to load audit feed"
            action={
              <Button
                variant="secondary"
                size="xs"
                icon={<RefreshCw className="w-3 h-3" />}
                onClick={() => void refreshAll()}
              >
                Retry
              </Button>
            }
          >
            {error}
          </AlertBanner>
        )}

        <SummaryMetricGrid items={summaryItems} columns={4} />

        <EmailFiltersBar
          searchQuery={searchInput}
          onSearchChange={setSearchInput}
          statusFilter={statusFilter}
          onStatusChange={setStatusFilter}
          routeTargetFilter={routeTargetFilter}
          onRouteTargetChange={setRouteTargetFilter}
        />

        {isLoading ? (
          <LoadingState label="Loading inbound email ledger..." variant="blue" />
        ) : emails.length === 0 ? (
          <EmptyState
            icon={<Inbox className="w-8 h-8" />}
            title="No matching inbound emails"
            description="Adjust your status, route target, or search filters, or forward a message to Iris to populate the audit feed."
            action={
              statusFilter !== 'all' || routeTargetFilter !== 'all' || searchInput.trim() !== '' ? (
                <Button
                  variant="secondary"
                  size="sm"
                  className="min-h-[44px]"
                  onClick={() => {
                    setStatusFilter('all');
                    setRouteTargetFilter('all');
                    setSearchInput('');
                  }}
                >
                  Clear Filters
                </Button>
              ) : undefined
            }
          />
        ) : (
          <div className="space-y-3 min-w-0">
            {emails.map((email) => (
              <EmailListRow
                key={email.id}
                email={email}
                onSelect={(id) => void openEmailDetail(id)}
              />
            ))}
          </div>
        )}

        <PaginationBar
          page={page}
          pageSize={pageSize}
          totalItems={total}
          onPageChange={setPage}
          loading={isLoading || isRefreshing}
          itemLabel="emails"
        />
      </div>

      <EmailDetailModal
        isOpen={isDetailOpen}
        onClose={closeEmailDetail}
        email={selectedEmail}
        isLoading={isDetailLoading}
        actionLoading={actionLoading}
        actionError={actionError}
        onRetry={handleRetry}
        onReroute={handleReroute}
        onIgnore={handleIgnore}
      />
    </AppShell>
  );
};
