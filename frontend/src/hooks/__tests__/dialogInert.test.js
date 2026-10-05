import { test } from 'node:test'
import assert from 'node:assert/strict'
import { holdInert, releaseInert } from '../useDialogFocus.js'

test('an_element_held_by_two_dialogs_stays_inert_until_both_let_go', () => {
  const element = { inert: false }
  holdInert(element)
  holdInert(element)
  releaseInert(element)
  assert.equal(element.inert, true)
  releaseInert(element)
  assert.equal(element.inert, false)
})

test('an_element_that_was_already_inert_is_left_inert', () => {
  const element = { inert: true }
  holdInert(element)
  releaseInert(element)
  assert.equal(element.inert, true)
})
