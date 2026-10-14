/* Goal-owned helper presentation comes only from the embedded plan tree. */
import { isHelperAwaitingParent, isWorkingHelperStatus } from './workingHelper.js'

const helperCount = count => `${count} ${count === 1 ? 'helper' : 'helpers'}`

export function goalHelpers(goal) {
  const helpers = []
  const seen = new Set()
  const visit = (node, inheritedTask = null) => {
    if (!node || seen.has(node.id)) return
    seen.add(node.id)
    const planTask = node.plan_task || inheritedTask
    helpers.push({ ...node, plan_task: planTask })
    ;(node.children || []).forEach(child => visit(child, planTask))
  }
  ;(goal?.plan?.delegations || []).forEach(node => visit(node))
  return helpers
}

/** Keep unrelated (or unproven) background work in its own Waiting panel. */
export function helpersOutsideGoal(backgroundHelpers, goal) {
  const owned = new Set(goalHelpers(goal).map(node => node.id))
  const items = backgroundHelpers?.items || []
  const matched = items.filter(item => owned.has(item.id)).length
  return {
    count: Math.max(0, (backgroundHelpers?.count || 0) - matched),
    items: items.filter(item => !owned.has(item.id)),
  }
}

/**
 * The Goal headline's helper clause: who acts next, never who merely exists.
 * A helper question is owed by its coordinating agent, so it never reads as
 * owner input; a saved owner card (`owner_input` handoff) outranks helper
 * work, which stays visible in the Goal details.
 */
export function goalHelperWaitingLabel(goal, { turnActive = false } = {}) {
  if (turnActive || !['active', 'paused'].includes(goal?.status)) return null
  if (goal.handoff?.kind === 'owner_input') return null
  const helpers = goalHelpers(goal)
  const working = helpers.filter(node => isWorkingHelperStatus(node.status)).length
  const asking = helpers.filter(isHelperAwaitingParent).length
  if (!working && !asking) return null
  const waiting = [
    working ? `Waiting on ${helperCount(working)}` : null,
    asking
      ? `${working ? asking : asking === 1 ? 'Helper' : helperCount(asking)} waiting for coordinating agent`
      : null,
  ].filter(Boolean).join(' · ')
  if (goal.status === 'paused' && goal.handoff?.kind !== 'automatic') return `On hold · ${waiting}`
  return goal.handoff?.kind === 'automatic'
    ? `${waiting} · resumes automatically`
    : waiting
}
