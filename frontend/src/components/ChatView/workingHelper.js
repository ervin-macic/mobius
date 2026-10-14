/* Whether a helper row still describes live work, and who acts next for it. */

// Statuses of a delegated helper that is still working. Its one activity row
// sits where it was launched and shows its final state once it settles.
const WORKING_STATUSES = new Set([
  'accepted', 'retrying', 'starting', 'running', 'resuming', 'paused',
])

export function isWorkingHelper(item) {
  return item?.type === 'helper_result' && WORKING_STATUSES.has(item.status)
}

export function isWorkingHelperStatus(status) {
  return WORKING_STATUSES.has(status)
}

// `needs_input` means the helper asked its direct parent agent (ask_parent)
// and stopped. That agent answers or escalates; the owner acts only through
// a saved owner card, so this state must never read as a request to the owner.
export const HELPER_AWAITING_PARENT_LABEL = 'Waiting for coordinating agent'

export function isHelperAwaitingParent(item) {
  return item?.status === 'needs_input'
}
