import { useCallback, useEffect, useRef, useState } from 'react';
import { isSessionExpiredError, useDebouncedValue } from '@yggdrasil-lab/ui';
import {
  ApiRequestError,
  fetchEmailDetail,
  fetchEmails,
  fetchEmailStats,
  ignoreEmail,
  rerouteEmail,
  retryEmail,
} from '../services/api';
import type {
  InboundEmailDetailResponse,
  InboundEmailRead,
  InboundEmailStatsResponse,
  RouteTargetType,
} from '../types/email';

export type EmailActionKind = 'retry' | 'reclassify' | 'reroute' | 'ignore' | null;

const PAGE_SIZE = 25;

interface MutationHandlersParams {
  refreshAll: () => Promise<void>;
  setSessionExpired: (expired: boolean) => void;
  setIsDetailOpen: (open: boolean) => void;
  setSelectedEmail: (email: InboundEmailDetailResponse | null) => void;
  setActionLoading: (kind: EmailActionKind) => void;
  setActionError: (msg: string | null) => void;
}

function useDetailMutationRunner({
  refreshAll,
  setSessionExpired,
  setIsDetailOpen,
  setSelectedEmail,
  setActionLoading,
  setActionError,
}: MutationHandlersParams) {
  return useCallback(
    async (kind: EmailActionKind, task: () => Promise<InboundEmailDetailResponse>) => {
      setActionLoading(kind);
      setActionError(null);
      try {
        const updated = await task();
        setSelectedEmail(updated);
        setSessionExpired(false);
        await refreshAll();
      } catch (err) {
        if (isSessionExpiredError(err)) {
          setSessionExpired(true);
          setIsDetailOpen(false);
        } else if (err instanceof ApiRequestError) {
          if (err.updatedEmail) {
            setSelectedEmail(err.updatedEmail);
            await refreshAll();
          }
          setActionError(err.message);
        } else {
          setActionError(err instanceof Error ? err.message : 'Action failed');
        }
      } finally {
        setActionLoading(null);
      }
    },
    [refreshAll, setActionError, setActionLoading, setIsDetailOpen, setSelectedEmail, setSessionExpired],
  );
}

function useEmailDetailActions(
  refreshAll: () => Promise<void>,
  setSessionExpired: (expired: boolean) => void,
) {
  const [isDetailOpen, setIsDetailOpen] = useState<boolean>(false);
  const [selectedEmail, setSelectedEmail] = useState<InboundEmailDetailResponse | null>(null);
  const [isDetailLoading, setIsDetailLoading] = useState<boolean>(false);
  const [actionLoading, setActionLoading] = useState<EmailActionKind>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const runMutation = useDetailMutationRunner({
    refreshAll,
    setSessionExpired,
    setIsDetailOpen,
    setSelectedEmail,
    setActionLoading,
    setActionError,
  });

  const openEmailDetail = useCallback(async (id: number) => {
    setIsDetailOpen(true);
    setIsDetailLoading(true);
    setActionError(null);
    setSelectedEmail(null);
    try {
      setSelectedEmail(await fetchEmailDetail(id));
      setSessionExpired(false);
    } catch (err) {
      if (isSessionExpiredError(err)) {
        setSessionExpired(true);
        setIsDetailOpen(false);
      } else {
        setActionError(err instanceof Error ? err.message : 'Failed to load email detail');
      }
    } finally {
      setIsDetailLoading(false);
    }
  }, [setSessionExpired]);

  const closeEmailDetail = useCallback(() => {
    if (actionLoading !== null) return;
    setIsDetailOpen(false);
    setActionError(null);
  }, [actionLoading]);

  const handleRetry = useCallback(
    (id: number, reclassify: boolean) =>
      runMutation(reclassify ? 'reclassify' : 'retry', () => retryEmail(id, reclassify)),
    [runMutation],
  );

  const handleReroute = useCallback(
    (id: number, routeTarget: RouteTargetType) =>
      runMutation('reroute', () => rerouteEmail(id, routeTarget)),
    [runMutation],
  );

  const handleIgnore = useCallback(
    (id: number) => runMutation('ignore', () => ignoreEmail(id)),
    [runMutation],
  );

  return {
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
  };
}

function useEmailFeedFilters() {
  const [page, setPage] = useState<number>(1);
  const [statusFilter, setStatusFilterState] = useState<string>('all');
  const [routeTargetFilter, setRouteTargetFilterState] = useState<string>('all');
  const [searchInput, setSearchInput] = useState<string>('');
  const debouncedQuery = useDebouncedValue(searchInput, 250);

  const setStatusFilter = useCallback((nextStatus: string) => {
    setStatusFilterState(nextStatus);
    setPage(1);
  }, []);

  const setRouteTargetFilter = useCallback((nextRoute: string) => {
    setRouteTargetFilterState(nextRoute);
    setPage(1);
  }, []);

  return {
    page,
    setPage,
    statusFilter,
    setStatusFilter,
    routeTargetFilter,
    setRouteTargetFilter,
    searchInput,
    setSearchInput,
    debouncedQuery,
  };
}

export function useIrisEmails() {
  const filters = useEmailFeedFilters();
  const [stats, setStats] = useState<InboundEmailStatsResponse | null>(null);
  const [emails, setEmails] = useState<InboundEmailRead[]>([]);
  const [total, setTotal] = useState<number>(0);
  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [isRefreshing, setIsRefreshing] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [sessionExpired, setSessionExpired] = useState<boolean>(false);

  const hasLoadedOnceRef = useRef<boolean>(false);
  const prevDebouncedQueryRef = useRef<string>(filters.debouncedQuery);

  const refreshAll = useCallback(async () => {
    if (!hasLoadedOnceRef.current) setIsLoading(true);
    else setIsRefreshing(true);
    setError(null);

    try {
      const [statsRes, listRes] = await Promise.all([
        fetchEmailStats(),
        fetchEmails({
          status: filters.statusFilter,
          route_target: filters.routeTargetFilter,
          q: filters.debouncedQuery,
          page: filters.page,
          page_size: PAGE_SIZE,
        }),
      ]);
      setStats(statsRes);
      setEmails(listRes.items);
      setTotal(listRes.total);
      setSessionExpired(false);
      hasLoadedOnceRef.current = true;
    } catch (err) {
      if (isSessionExpiredError(err)) setSessionExpired(true);
      else setError(err instanceof Error ? err.message : 'Failed to load inbound emails');
    } finally {
      setIsLoading(false);
      setIsRefreshing(false);
    }
  }, [filters.statusFilter, filters.routeTargetFilter, filters.debouncedQuery, filters.page]);

  useEffect(() => {
    if (prevDebouncedQueryRef.current !== filters.debouncedQuery) {
      prevDebouncedQueryRef.current = filters.debouncedQuery;
      if (filters.page !== 1) {
        filters.setPage(1);
        return;
      }
    }
    void refreshAll();
  }, [filters.debouncedQuery, filters.page, filters.setPage, refreshAll]);

  const detailActions = useEmailDetailActions(refreshAll, setSessionExpired);

  return {
    stats,
    emails,
    total,
    pageSize: PAGE_SIZE,
    ...filters,
    isLoading,
    isRefreshing,
    error,
    sessionExpired,
    refreshAll,
    ...detailActions,
  };
}
