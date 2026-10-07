/**
 * First-open refresh for event-fed caches restored from persistence.
 *
 * The system stream's first open of a page load does not invalidate event-fed
 * caches: mount-time reads already fetched them. Persisted entries are the
 * exception. They are restored with their original fetch time, so a reload
 * inside their staleTime makes no request and would miss every event published
 * while no page was listening. Only provider status is both event-fed and
 * persisted (see PERSISTED_FULL_KEYS in queryClient.js); refresh it when it was
 * fetched before this page loaded.
 */
export function invalidateRestoredEventFedCaches(queryClient, pageLoadedAt) {
  return [queryClient.invalidateQueries({
    queryKey: ['auth', 'providers', 'status'],
    exact: true,
    predicate: (query) => query.state.dataUpdatedAt > 0
      && query.state.dataUpdatedAt < pageLoadedAt,
  })]
}
