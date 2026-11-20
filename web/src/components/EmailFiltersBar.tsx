import React from 'react';
import { ChipSelector, SearchInput } from '@yggdrasil-lab/ui';
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

export const EmailFiltersBar: React.FC<EmailFiltersBarProps> = ({
  searchQuery,
  onSearchChange,
  statusFilter,
  onStatusChange,
  routeTargetFilter,
  onRouteTargetChange,
}) => {
  return (
    <div className="rounded-2xl bg-card/70 backdrop-blur-md border border-border/80 p-3.5 shadow-lg space-y-3">
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
          <label htmlFor="route-target-filter" className="sr-only">
            Filter by route target
          </label>
          <select
            id="route-target-filter"
            data-testid="route-target-filter"
            value={routeTargetFilter}
            onChange={(e) => onRouteTargetChange(e.target.value)}
            className="w-full min-h-[44px] px-3 py-2 bg-background border border-border rounded-xl text-base sm:text-xs text-slate-200 focus:outline-none focus:border-blue-500 transition-colors"
          >
            <option value="all">All Routes</option>
            {ROUTE_TARGET_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
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
    </div>
  );
};
