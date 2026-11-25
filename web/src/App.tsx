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
  type ThemeVariant,
} from '@yggdrasil-lab/ui';
import { AlertTriangle, CheckCircle2, Clock, Inbox, Mail, RefreshCw } from 'lucide-react';
import { EmailDetailModal } from './components/EmailDetailModal';
import { EmailFiltersBar } from './components/EmailFiltersBar';
import { EmailListRow } from './components/EmailListRow';
import { useIrisEmails } from './hooks/useIrisEmails';
import type { InboundEmailRead, InboundEmailStatsResponse } from './types/email';

interface KpiTileSpec {
  id: string;
  filterValue: string;
  label: string;
  subvalue: string;
  variant: ThemeVariant;
  valueClassName?: string;
  icon: React.ReactNode;
  readCount: (stats: InboundEmailStatsResponse | null) => number;
}

const KPI_TILE_SPECS: KpiTileSpec[] = [
  {
    id: 'total',
    filterValue: 'all',
    label: 'Total Captured',
    subvalue: 'All inbound mail',
    variant: 'blue',
    icon: <Inbox className="w-3 h-3 text-sky-400" />,
    readCount: (stats) => stats?.total ?? 0,
  },
  {
    id: 'needs_review',
    filterValue: 'needs_review',
    label: 'Needs Review',
    subvalue: 'Operator queue',
    variant: 'amber',
    valueClassName: 'text-amber-400',
    icon: <Clock className="w-3 h-3 text-amber-400" />,
    readCount: (stats) => stats?.by_status?.needs_review ?? 0,
  },
  {
    id: 'failed',
    filterValue: 'failed',
    label: 'Failed',
    subvalue: 'Needs retry',
    variant: 'amber',
    valueClassName: 'text-rose-400',
    icon: <AlertTriangle className="w-3 h-3 text-rose-400" />,
    readCount: (stats) => stats?.by_status?.failed ?? 0,
  },
  {
    id: 'dispatched',
    filterValue: 'dispatched',
    label: 'Dispatched',
    subvalue: 'Delivered cleanly',
    variant: 'emerald',
    valueClassName: 'text-emerald-400',
    icon: <CheckCircle2 className="w-3 h-3 text-emerald-400" />,
    readCount: (stats) => stats?.by_status?.dispatched ?? 0,
  },
];

function buildSummaryItems(
  stats: InboundEmailStatsResponse | null,
  statusFilter: string,
  setStatusFilter: (status: string) => void,
): SummaryMetricItem[] {
  return KPI_TILE_SPECS.map((spec) => {
    const isActive = statusFilter === spec.filterValue;
    const nextFilter = spec.filterValue !== 'all' && isActive ? 'all' : spec.filterValue;

    return {
      id: spec.id,
      label: spec.label,
      icon: spec.icon,
      value: spec.readCount(stats),
      valueClassName: spec.valueClassName,
      subvalue: spec.subvalue,
      variant: spec.variant,
      active: isActive,
      ariaLabel: `Filter by ${spec.label}`,
      testId: `kpi-tile-${spec.id}`,
      onClick: () => setStatusFilter(nextFilter),
    };
  });
}

interface AuditFeedBodyProps {
  isLoading: boolean;
  emails: InboundEmailRead[];
  hasActiveFilters: boolean;
  onResetFilters: () => void;
  onSelectEmail: (id: number) => void;
}

const AuditFeedBody: React.FC<AuditFeedBodyProps> = ({
  isLoading,
  emails,
  hasActiveFilters,
  onResetFilters,
  onSelectEmail,
}) => {
  if (isLoading) {
    return <LoadingState label="Loading inbound email ledger..." variant="blue" />;
  }
  if (emails.length === 0) {
    return (
      <EmptyState
        icon={<Inbox className="w-8 h-8" />}
        title="No matching inbound emails"
        description="Adjust your status, route target, or search filters, or forward a message to Iris to populate the audit feed."
        action={
          hasActiveFilters ? (
            <Button variant="secondary" size="sm" className="min-h-[44px]" onClick={onResetFilters}>
              Clear Filters
            </Button>
          ) : undefined
        }
      />
    );
  }
  return (
    <div className="space-y-3 min-w-0">
      {emails.map((email) => (
        <EmailListRow key={email.id} email={email} onSelect={onSelectEmail} />
      ))}
    </div>
  );
};

const AuditFeedErrorBanner: React.FC<{
  error: string | null;
  onRetry: () => void;
}> = ({ error, onRetry }) => {
  if (!error) return null;
  return (
    <AlertBanner
      variant="error"
      title="Unable to load audit feed"
      action={
        <Button
          variant="secondary"
          size="xs"
          icon={<RefreshCw className="w-3 h-3" />}
          onClick={onRetry}
        >
          Retry
        </Button>
      }
    >
      {error}
    </AlertBanner>
  );
};

export const App: React.FC = () => {
  const iris = useIrisEmails();
  const hasActiveFilters =
    iris.statusFilter !== 'all' ||
    iris.routeTargetFilter !== 'all' ||
    iris.searchInput.trim() !== '';
  const summaryItems = buildSummaryItems(iris.stats, iris.statusFilter, iris.setStatusFilter);

  const resetFilters = () => {
    iris.setStatusFilter('all');
    iris.setRouteTargetFilter('all');
    iris.setSearchInput('');
  };

  return (
    <AppShell
      header={
        <AppHeader
          variant="blue"
          title="Iris Gateway"
          subtitle="Inbound Email Audit & Routing"
          brandIcon={<Mail className="w-4 h-4" />}
          onRefresh={() => void iris.refreshAll()}
          isRefreshing={iris.isRefreshing}
        />
      }
    >
      <div className="space-y-4 min-w-0">
        {iris.sessionExpired && <SessionExpiredBanner />}
        <AuditFeedErrorBanner error={iris.error} onRetry={() => void iris.refreshAll()} />
        <SummaryMetricGrid items={summaryItems} columns={4} variant="blue" />
        <EmailFiltersBar
          searchQuery={iris.searchInput}
          onSearchChange={iris.setSearchInput}
          statusFilter={iris.statusFilter}
          onStatusChange={iris.setStatusFilter}
          routeTargetFilter={iris.routeTargetFilter}
          onRouteTargetChange={iris.setRouteTargetFilter}
        />
        <AuditFeedBody
          isLoading={iris.isLoading}
          emails={iris.emails}
          hasActiveFilters={hasActiveFilters}
          onResetFilters={resetFilters}
          onSelectEmail={(id) => void iris.openEmailDetail(id)}
        />
        <PaginationBar
          page={iris.page}
          pageSize={iris.pageSize}
          totalItems={iris.total}
          onPageChange={iris.setPage}
          loading={iris.isLoading || iris.isRefreshing}
          itemLabel="emails"
        />
      </div>
      <EmailDetailModal
        isOpen={iris.isDetailOpen}
        onClose={iris.closeEmailDetail}
        email={iris.selectedEmail}
        isLoading={iris.isDetailLoading}
        actionLoading={iris.actionLoading}
        actionError={iris.actionError}
        onRetry={iris.handleRetry}
        onReroute={iris.handleReroute}
        onIgnore={iris.handleIgnore}
      />
    </AppShell>
  );
};
