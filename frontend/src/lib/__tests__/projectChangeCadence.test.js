import test from 'node:test'
import assert from 'node:assert/strict'

import {
  PROJECT_CHANGES_ACTIVE_MS,
  PROJECT_CHANGES_FAILURE_MAX_MS,
  PROJECT_CHANGES_IDLE_MAX_MS,
  nextProjectChangesDelay,
} from '../projectChangeCadence.js'

test('an idle open project stretches its change poll to a bounded idle rate', () => {
  let delay = PROJECT_CHANGES_ACTIVE_MS
  const waits = []
  for (let i = 0; i < 10; i += 1) {
    delay = nextProjectChangesDelay(delay, 'unchanged')
    waits.push(delay)
  }
  assert.ok(waits.every((wait, i) => i === 0 || wait >= waits[i - 1]))
  assert.equal(waits.at(-1), PROJECT_CHANGES_IDLE_MAX_MS)
})

test('any observed change returns the poll to the active rate', () => {
  assert.equal(
    nextProjectChangesDelay(PROJECT_CHANGES_IDLE_MAX_MS, 'changed'),
    PROJECT_CHANGES_ACTIVE_MS,
  )
})

test('failures back off further than idle reads but stay bounded', () => {
  let delay = PROJECT_CHANGES_ACTIVE_MS
  for (let i = 0; i < 10; i += 1) delay = nextProjectChangesDelay(delay, 'failed')
  assert.equal(delay, PROJECT_CHANGES_FAILURE_MAX_MS)
  assert.equal(
    nextProjectChangesDelay(delay, 'unchanged'),
    PROJECT_CHANGES_IDLE_MAX_MS,
  )
})
