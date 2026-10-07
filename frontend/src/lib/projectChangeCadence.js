// Cadence for an open project's durable change-cursor poll.
//
// The cursor is the only change feed for invited editors, and it also carries
// owner-side facts the system stream does not publish (an agent run finishing
// after direct filesystem edits). So it keeps running, but it is quick only
// while the project is actually changing: each empty read stretches the wait,
// and any observed change (polled or pushed) snaps it back to the active rate.
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
