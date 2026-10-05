import test from 'node:test'
import assert from 'node:assert/strict'
import { formatGoalDuration, goalHistoryViewModel } from '../goalHistory.js'

test('Goal history formats useful elapsed time compactly', () => {
  assert.equal(formatGoalDuration(14.6), '15s')
  assert.equal(formatGoalDuration(125), '2m 5s')
  assert.equal(formatGoalDuration(7260), '2h 1m')
})

test('Goal history summarizes a terminal outcome and its plan', () => {
  assert.deepEqual(goalHistoryViewModel({
    objective: ' Ship the Goal experience ',
    status: 'completed',
    duration_seconds: 125,
    plan: {
      summary: { completed: 3, total: 3 },
      tasks: [{ id: 'verify', title: 'Verify', status: 'completed' }],
    },
  }), {
    objective: 'Ship the Goal experience',
    completed: true,
    kicker: 'Goal completed',
    ariaLabel: 'Completed goal: Ship the Goal experience',
    metadata: '3 of 3 steps complete · 2m 5s',
    hasPlan: true,
    reason: '',
  })
})

test('Goal history preserves neutral labels for legacy failed snapshots', () => {
  assert.equal(goalHistoryViewModel({ objective: 'Still working', status: 'active' }), null)
  assert.deepEqual(goalHistoryViewModel({
    objective: 'Needs repair', status: 'failed', duration_seconds: null,
  }), {
    objective: 'Needs repair',
    completed: false,
    kicker: 'Goal needs attention',
    ariaLabel: 'Goal needing attention: Needs repair',
    reason: '',
    metadata: '',
    hasPlan: false,
  })
})

test('terminal Goal outcomes retain their honest result in history', () => {
  const cancelled = goalHistoryViewModel({ objective: 'Ship', status: 'cancelled', result: { reason: 'Owner stopped it' } })
  assert.equal(cancelled.kicker, 'Goal cancelled')
  assert.equal(cancelled.reason, 'Owner stopped it')
  const impossible = goalHistoryViewModel({ objective: 'Ship', status: 'cannot_complete', result: 'Dependency unavailable' })
  assert.equal(impossible.kicker, 'Goal cannot complete')
  assert.equal(impossible.reason, 'Dependency unavailable')
})
