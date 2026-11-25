import React from 'react';
import { ChipSelector, GlassCard, SearchInput, SelectInput } from '@yggdrasil-lab/ui';
import { ROUTE_TARGET_OPTIONS } from '../types/email';

export interface EmailFiltersBarProps {
  searchQuery: string;
  onSearchChange: (value: string) => void;
  statusFilter: string;
  onStatusChange: (value: string) => void;
  routeTargetFilter: string;
  onRouteTargetChange: (value: string) => void;
}

const STATUS_CHIP_OPTIONS = [
  { value: 'all', label: 'All' },
  { value: 'needs_review', label: 'Needs Review' },
  { value: 'failed', label: 'Failed' },
  { value: 'dispatched', label: 'Dispatched' },
  { value: 'ignored', label: 'Ignored' },
];

const ROUTE_FILTER_OPTIONS = [
  { value: 'all', label: 'All Routes' },
  ...ROUTE_TARGET_OPTIONS,
];

export const EmailFiltersBar: React.FC<EmailFiltersBarProps> = ({
  searchQuery,
  onSearchChange,
  statusFilter,
  onStatusChange,
  routeTargetFilter,
  onRouteTargetChange,
}) => {
  return (
    <GlassCard padding="sm" className="space-y-3">
      <div className="flex flex-col sm:flex-row gap-2.5">
        <SearchInput
          value={searchQuery}
          onChange={onSearchChange}
          placeholder="Search subject, sender, Message-ID, or excerpt..."
          variant="blue"
          className="flex-1 min-w-0 [&_input]:min-h-[44px]"
          data-testid="email-search-input"
        />

        <div className="sm:w-56 shrink-0">
          <SelectInput
            id="route-target-filter"
            data-testid="route-target-filter"
            aria-label="Filter by route target"
            variant="blue"
            value={routeTargetFilter}
            onChange={(e) => onRouteTargetChange(e.target.value)}
            options={ROUTE_FILTER_OPTIONS}
          />
        </div>
      </div>

      <ChipSelector
        options={STATUS_CHIP_OPTIONS}
        selected={statusFilter}
        onChange={onStatusChange}
        variant="blue"
        size="sm"
        className="[&>button]:min-h-[44px] [&>button]:px-3.5"
      />
    </GlassCard>
  );
};
