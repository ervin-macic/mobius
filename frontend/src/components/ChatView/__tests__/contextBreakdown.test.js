/* /context rows add up to the provider's total and never invent a size. */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  COMPACT_SUGGESTION_PERCENT,
  contextBreakdown,
  contextNeverStarted,
  suggestsCompaction,
} from '../contextBreakdown.js'

const parts = [
  { key: 'instructions', tokens: 9800 },
  { key: 'skills', tokens: 3100 },
  { key: 'recent_chats', tokens: 1600 },
  { key: 'app_context', tokens: 0 },
  { key: 'compaction', tokens: 0 },
]

const sum = rows => rows.reduce((total, row) => total + row.tokens, 0)

test('measured rows, conversation and free space add up to the window', () => {
  const breakdown = contextBreakdown({ parts, tokens: { used: 43000, maximum: 200000 } })
  assert.equal(breakdown.state, 'measured')
  assert.deepEqual(breakdown.parts.map(part => part.key), ['instructions', 'skills', 'recent_chats'])
  assert.equal(sum(breakdown.parts) + breakdown.conversation, 43000)
  assert.equal(breakdown.used + breakdown.free, 200000)
  assert.equal(breakdown.conversation, 43000 - 9800 - 3100 - 1600)
  assert.equal(breakdown.percent, 22)
  assert.equal(breakdown.scaled, false)
})

test('estimates above a small total are scaled to fit, so conversation is never negative', () => {
  const breakdown = contextBreakdown({ parts, tokens: { used: 7000, maximum: 200000 } })
  assert.equal(breakdown.scaled, true)
  assert.ok(breakdown.conversation >= 0)
  assert.equal(sum(breakdown.parts) + breakdown.conversation, 7000)
  // The parts keep their proportions when scaled.
  const [instructions, skills] = breakdown.parts
  assert.ok(instructions.tokens > skills.tokens)
})

test('a chat that has not started shows only what its first reply will load', () => {
  const breakdown = contextBreakdown({
    parts,
    tokens: { used: 0, maximum: 200000 },
    noSession: true,
  })
  assert.equal(breakdown.state, 'not_started')
  assert.equal(breakdown.conversation, undefined)
  assert.deepEqual(breakdown.parts.map(part => part.tokens), [9800, 3100, 1600])
})

test('an unknown total shows the measured parts and no conversation size', () => {
  const breakdown = contextBreakdown({ parts, tokens: null })
  assert.equal(breakdown.state, 'unknown')
  assert.equal(breakdown.conversation, undefined)
  assert.equal(breakdown.parts.length, 3)
})

test('a part key the panel does not know stays inside the conversation total', () => {
  const breakdown = contextBreakdown({
    parts: [...parts, { key: 'future_part', tokens: 5000 }],
    tokens: { used: 43000, maximum: 200000 },
  })
  assert.ok(!breakdown.parts.some(part => part.key === 'future_part'))
  assert.equal(breakdown.conversation, 43000 - 9800 - 3100 - 1600)
})

test('a total reported above the window is held to the window', () => {
  const breakdown = contextBreakdown({ parts, tokens: { used: 250000, maximum: 200000 } })
  assert.equal(breakdown.used, 200000)
  assert.equal(breakdown.free, 0)
  assert.equal(breakdown.percent, 100)
})

test('a cleared session id is not mistaken for a chat that never started', () => {
  // Compaction and provider handoffs clear the resumable session id on chats
  // full of history. Their usage is unknown until the next turn — never
  // "nothing is in context".
  assert.equal(contextNeverStarted({ started: true, providerSessionId: null }), false)
  const breakdown = contextBreakdown({ parts, tokens: null, noSession: false })
  assert.equal(breakdown.state, 'unknown')
  // Only a provisional chat that has not sent its first message is empty.
  assert.equal(contextNeverStarted({ started: false, providerSessionId: null }), true)
  // A live session always means the chat started, whatever the flag says.
  assert.equal(contextNeverStarted({ started: false, providerSessionId: 'sess-1' }), false)
})

test('compaction is offered only once the context is nearly full', () => {
  const at = percent => contextBreakdown({
    parts,
    tokens: { used: percent * 2000, maximum: 200000 },
  })
  assert.equal(suggestsCompaction(at(COMPACT_SUGGESTION_PERCENT - 1)), false)
  assert.equal(suggestsCompaction(at(COMPACT_SUGGESTION_PERCENT)), true)
  assert.equal(suggestsCompaction(contextBreakdown({ parts, tokens: null })), false)
})
