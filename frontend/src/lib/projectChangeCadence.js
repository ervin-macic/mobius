// Cadence for an open project's durable change-cursor poll.
//
// The cursor is the only change feed for invited editors (they have no system
// event stream) and the owner's repair path for missed events. It stays quick
// while changes keep arriving: each empty read stretches the wait, and any
// observed change snaps it back to the active rate.
export const PROJECT_CHANGES_ACTIVE_MS = 2_500
export const PROJECT_CHANGES_IDLE_MAX_MS = 20_000
export const PROJECT_CHANGES_FAILURE_MAX_MS = 30_000

export function nextProjectChangesDelay(previous, outcome) {
  if (outcome === 'changed') return PROJECT_CHANGES_ACTIVE_MS
  if (outcome === 'failed') {
    return Math.min(previous * 2, PROJECT_CHANGES_FAILURE_MAX_MS)
  }
  return Math.min(Math.round(previous * 1.5), PROJECT_CHANGES_IDLE_MAX_MS)
}
