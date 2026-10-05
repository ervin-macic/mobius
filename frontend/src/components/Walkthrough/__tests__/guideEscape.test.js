import { test } from 'node:test'
import assert from 'node:assert/strict'
import { escapeShouldDismissGuide } from '../guideEscape.js'

const target = matches => ({ closest: () => matches })

test('escape_on_the_guide_itself_dismisses_it', () => {
  assert.equal(escapeShouldDismissGuide({ target: target(null) }), true)
})

test('escape_in_a_text_field_only_backs_out_of_the_field', () => {
  assert.equal(escapeShouldDismissGuide({ target: target({}) }), false)
})

test('escape_during_ime_composition_is_ignored', () => {
  assert.equal(escapeShouldDismissGuide({ isComposing: true, target: target(null) }), false)
  assert.equal(escapeShouldDismissGuide({ keyCode: 229, target: target(null) }), false)
})
