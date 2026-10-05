import { test } from 'node:test'
import assert from 'node:assert/strict'
import { currentChatAnnouncement, currentProgressGoal, goalContinuationHandoff } from '../chatHandoffPresentation.js'

const held = {
  id: 'goal-a', revision: 7, objective: 'Review the change', status: 'paused',
  pause_reason: 'deferred', hold_reason: 'Paid checks were deferred.',
  resumable: true, handoff: { kind: 'none' },
}

test('deferred work has a visible continuation without claiming new permission', () => {
  const handoff = goalContinuationHandoff(held)
  assert.equal(handoff.actionLabel, 'Continue this work')
  assert.equal(handoff.description, held.hold_reason)
  assert.match(handoff.boundary, /does not approve/)
  assert.deepEqual(held.handoff, { kind: 'none' })
})

test('saved questions, recovery controls and unrelated live work never get a competing Continue', () => {
  for (const state of [
    { turnActive: true }, { hasPendingQuestion: true }, { hasPendingResume: true },
    { chatHandoff: 'owner_input' }, { chatHandoff: 'automatic' },
  ]) assert.equal(goalContinuationHandoff(held, state), null)
  assert.equal(goalContinuationHandoff({ ...held, handoff: { kind: 'owner_input' } }), null)
  assert.equal(goalContinuationHandoff({ ...held, handoff: { kind: 'automatic' } }), null)
})

test('unknown interruption is not attributed to the owner or mistaken for deliberate deferral', () => {
  const handoff = goalContinuationHandoff({ ...held, pause_reason: 'unknown', hold_reason: null })
  assert.equal(handoff.actionLabel, 'Resume this work')
  assert.match(handoff.description, /not complete/)
})

test('terminal Goal receipts are history, never the next request’s progress', () => {
  for (const status of ['completed', 'cannot_complete', 'cancelled']) {
    const goal = { ...held, status }
    assert.equal(currentProgressGoal(goal), null)
    assert.equal(currentProgressGoal(goal, { turnActive: true }), null)
    assert.equal(goalContinuationHandoff(goal), null)
    assert.equal(currentChatAnnouncement({ goal, turnActive: true }), 'Assistant is working.')
    assert.equal(currentChatAnnouncement({ goal, hasAnswer: true }), 'Response ready.')
  }
  assert.equal(currentProgressGoal(held, { turnActive: true }), null)
  assert.equal(currentProgressGoal(held), held)
  const active = { ...held, status: 'active' }
  assert.equal(currentProgressGoal(active, { turnActive: true }), active)
})

test('current owner card and automatic wake take precedence over retained history', () => {
  assert.match(currentChatAnnouncement({ goal: held, turnActive: true, hasPendingQuestion: true }), /^Waiting for you/)
  assert.match(currentChatAnnouncement({ goal: held, chatHandoff: 'owner_input' }), /^Waiting for you/)
  assert.match(currentChatAnnouncement({ goal: held, chatHandoff: 'automatic' }), /continue automatically/)
  assert.match(currentChatAnnouncement({ goal: held, chatHandoff: 'on_hold' }), /^On hold/)
  assert.equal(currentChatAnnouncement({ goal: held, recoveryStatus: 'Connection lost.' }), 'Connection lost.')
})

test('an existing recovery surface owns continuation after a live turn settles', () => {
  for (const turnActive of [true, false]) {
    assert.equal(goalContinuationHandoff(held, { turnActive, hasPendingResume: true, chatHandoff: 'recovery' }), null)
  }
})
