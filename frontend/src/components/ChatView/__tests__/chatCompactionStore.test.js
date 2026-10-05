import { afterEach, test } from 'node:test'
import assert from 'node:assert/strict'
import {
  chatCompactingKind,
  compactionNotice,
  resetChatCompactionForTests,
  setChatCompacting,
  subscribeChatCompaction,
} from '../chatCompactionStore.js'

afterEach(() => resetChatCompactionForTests())

test('every view of a chat shares the server rebuild window', () => {
  let calls = 0
  const unsubscribe = subscribeChatCompaction(() => { calls += 1 })
  setChatCompacting('chat-1', 'compact')
  assert.equal(chatCompactingKind('chat-1'), 'compact')
  assert.equal(chatCompactingKind('chat-2'), null)
  setChatCompacting('chat-1', 'compact')
  assert.equal(calls, 1)
  setChatCompacting('chat-1', null)
  assert.equal(chatCompactingKind('chat-1'), null)
  assert.equal(calls, 2)
  unsubscribe()
})

test('the notice explains why a new message is waiting', () => {
  assert.match(compactionNotice('compact'), /Compacting.*send when it finishes/)
  assert.match(compactionNotice('provider_switch'), /Switching.*send when it finishes/)
  assert.equal(compactionNotice(null), null)
})

test('the view whose Send the switch blocks shows no contradicting notice', () => {
  assert.equal(compactionNotice('provider_switch', { sendBlockedBySwitch: true }), null)
  assert.match(
    compactionNotice('provider_switch', { sendBlockedBySwitch: false }),
    /Switching.*send when it finishes/,
  )
})
