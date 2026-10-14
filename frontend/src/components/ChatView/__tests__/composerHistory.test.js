import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  COMPOSER_DRAFT_STATE,
  composerHistoryDirection,
  composerHistoryEntries,
  composerHistoryFromMessages,
  composerHistoryKeyPlan,
  composerHistoryPosition,
  composerHistoryProbeReachedBoundary,
  composerQueuedEntries,
  moveComposerHistory,
  openComposerHistoryEntry,
  returnToComposerDraft,
} from '../composerHistory.js'

function key(keyName, {
  selectionStart = 0,
  selectionEnd = selectionStart,
  ...overrides
} = {}) {
  return {
    key: keyName,
    target: { selectionStart, selectionEnd },
    ...overrides,
  }
}

const sent = (key, text) => ({ key, kind: 'sent', text })
const queued = (key, text) => ({ key, kind: 'queued', text })

const ENTRIES = [
  sent('s1', 'Fix the colour picker on mobile'),
  sent('s2', 'Add a test for the list menu'),
  queued('q1', 'Then check the dark theme'),
  queued('q2', 'Also update the changelog'),
]

function walk(state, value, ...directions) {
  let current = { state, value }
  for (const direction of directions) {
    current = moveComposerHistory(current.state, ENTRIES, direction, current.value)
  }
  return current
}

test('history contains only visible owner-authored text, keyed by message cid', () => {
  assert.deepEqual(composerHistoryFromMessages([
    { role: 'user', content: 'first', cid: 'c1' },
    { role: 'assistant', content: 'reply' },
    { role: 'user', content: 'hidden', hidden: true },
    { role: 'user', content: 'continue', kind: 'auto_continuation' },
    { role: 'user', content: 'continue', kind: 'continuation' },
    { role: 'user', content: '   ' },
    {
      role: 'user',
      cid: 'c2',
      content: [
        'review this',
        '',
        '[Files in this session:',
        '- brief.txt → /data/chats/private/uploads/brief.txt (text/plain, 1 KB)]',
      ].join('\n'),
    },
    { role: 'user', content: 'second', ts: 42 },
  ]), [
    sent('c1', 'first'),
    sent('c2', 'review this'),
    sent('sent-42', 'second'),
  ])
})

test('an added side question is context, so ↑ never offers it to resend', () => {
  assert.deepEqual(composerHistoryFromMessages([
    { role: 'user', content: 'first' },
    {
      role: 'user',
      kind: 'side_question',
      content: 'Side question: what do the tests cover?\n\nAnswer (from a temporary copy of this chat): Saving and moving cards.',
      side_question: { question: 'what do the tests cover?', answer: 'Saving and moving cards.' },
    },
  ]), [sent('sent-0', 'first')])
})

test('queued messages join the newest end, except rejected rows and rows that already started', () => {
  const queuedRows = composerQueuedEntries([
    { cid: 'q1', content: 'Then check the dark theme' },
    { cid: 'kept', content: 'Send rejected', recoveryAvailable: true },
    { cid: 's2', content: 'Add a test for the list menu' },
    { cid: 'q2', content: 'Also update the changelog' },
  ])
  assert.deepEqual(
    composerHistoryEntries([sent('s1', 'one'), sent('s2', 'Add a test for the list menu')], queuedRows)
      .map(entry => `${entry.kind}:${entry.key}`),
    ['sent:s1', 'sent:s2', 'queued:q1', 'queued:q2'],
  )
})

test('↑ reaches queued messages first, newest first, before sent ones', () => {
  const steps = []
  let current = { state: COMPOSER_DRAFT_STATE, value: 'my draft' }
  for (let i = 0; i < 4; i += 1) {
    current = moveComposerHistory(current.state, ENTRIES, 'older', current.value)
    steps.push(current.value)
  }
  assert.deepEqual(steps, [
    'Also update the changelog',
    'Then check the dark theme',
    'Add a test for the list menu',
    'Fix the colour picker on mobile',
  ])
  assert.equal(moveComposerHistory(current.state, ENTRIES, 'older', current.value), null,
    'the oldest message is the end of the list')
})

test('typing in a recalled message keeps the draft, and ↓ restores it', () => {
  const recalled = walk(COMPOSER_DRAFT_STATE, 'Also update the README', 'older', 'older', 'older')
  assert.equal(recalled.value, 'Add a test for the list menu')
  // Typing changes only what the box shows; it is not a reason to leave.
  const back = walk(recalled.state, `${recalled.value} on small phones`, 'newer', 'newer', 'newer')
  assert.equal(back.state.key, null)
  assert.equal(back.value, 'Also update the README')
})

test('edits survive moving away and back, and Esc drops them', () => {
  const recalled = walk(COMPOSER_DRAFT_STATE, 'draft', 'older', 'older', 'older')
  const edited = `${recalled.value} on small phones`
  const atDraft = walk(recalled.state, edited, 'newer', 'newer', 'newer')
  assert.equal(atDraft.value, 'draft')

  const again = walk(atDraft.state, atDraft.value, 'older', 'older', 'older')
  assert.equal(again.value, edited, 'the edit is still there after returning to the draft')

  const escaped = returnToComposerDraft(again.state, { discardEdits: true })
  assert.equal(escaped.value, 'draft')
  assert.equal(walk(escaped.state, escaped.value, 'older', 'older', 'older').value,
    'Add a test for the list menu', 'Esc dropped the edit')
})

test('restoring the original text is not kept as an edit', () => {
  const recalled = walk(COMPOSER_DRAFT_STATE, '', 'older')
  const moved = moveComposerHistory(recalled.state, ENTRIES, 'older', recalled.value)
  assert.deepEqual(moved.state.edits, {})
})

test('↓ past the newest entry restores an exact multiline draft', () => {
  const draft = 'unfinished first line\nand the second line'
  const recalled = moveComposerHistory(COMPOSER_DRAFT_STATE, ENTRIES, 'older', draft)
  assert.equal(recalled.state.draft, draft)
  assert.deepEqual(moveComposerHistory(recalled.state, ENTRIES, 'newer', recalled.value), {
    state: { ...COMPOSER_DRAFT_STATE, edits: {} },
    value: draft,
  })
})

test('↓ does nothing until history browsing has started', () => {
  assert.equal(moveComposerHistory(COMPOSER_DRAFT_STATE, ENTRIES, 'newer', 'draft'), null)
})

test('the pencil opens a queued message with the draft set aside, and saving drops only its edit', () => {
  const opened = openComposerHistoryEntry(COMPOSER_DRAFT_STATE, ENTRIES, 'q1', 'Then deploy it')
  assert.equal(opened.value, 'Then check the dark theme')
  assert.deepEqual(composerHistoryPosition(opened.state, ENTRIES), {
    kind: 'queued',
    entry: ENTRIES[2],
    queuedNumber: 1,
    queuedCount: 2,
    draft: 'Then deploy it',
  })
  const other = moveComposerHistory(opened.state, ENTRIES, 'older', 'Then check the dark theme and the README')
  const reopened = openComposerHistoryEntry(other.state, ENTRIES, 'q1', 'Add a test, edited')
  assert.equal(reopened.value, 'Then check the dark theme and the README')

  const saved = returnToComposerDraft(reopened.state)
  assert.equal(saved.value, 'Then deploy it')
  assert.deepEqual(saved.state.edits, { s2: 'Add a test, edited' },
    'the saved message forgets its edit; other kept edits stay')
})

test('a queued message that starts while open is reported as started, not as a sent one', () => {
  const opened = openComposerHistoryEntry(COMPOSER_DRAFT_STATE, ENTRIES, 'q2', '')
  const afterStart = [...ENTRIES.slice(0, 2), sent('q2', 'Also update the changelog'), ENTRIES[2]]
  assert.equal(composerHistoryPosition(opened.state, afterStart).kind, 'started')
  assert.equal(composerHistoryPosition(opened.state, ENTRIES.slice(0, 3)).kind, 'started',
    'missing from both lists while the transcript catches up')
  assert.equal(composerHistoryPosition({ ...opened.state, started: true }, ENTRIES).kind, 'started',
    'a save that reports the message already started')
})

test('↑/↓ in the middle of multi-line text move the caret natively', () => {
  const value = 'first line\nsecond line\nthird line'
  const middle = { selectionStart: 14 }
  assert.equal(composerHistoryKeyPlan(key('ArrowUp', middle), { direction: 'older', value }), null)
  assert.equal(composerHistoryKeyPlan(key('ArrowDown', middle), { direction: 'newer', value }), null)
  assert.equal(composerHistoryKeyPlan(key('ArrowUp', { selectionStart: 3 }), {
    direction: 'older', value,
  }).when, 'after-native', 'the top line waits for the browser to show it is the visual top')
  assert.equal(composerHistoryKeyPlan(key('ArrowDown', { selectionStart: value.length }), {
    direction: 'newer', value,
  }).when, 'after-native')
})

test('an empty box switches at once; a non-empty one leaves the first move to the browser', () => {
  assert.deepEqual(composerHistoryKeyPlan(key('ArrowUp'), { direction: 'older', value: '' }), { when: 'now' })
  const value = 'a long visually wrapped draft without source newlines'
  assert.equal(composerHistoryKeyPlan(key('ArrowUp', { selectionStart: value.length }), {
    direction: 'older', value,
  }).when, 'after-native')
})

test('a native probe switches only when the caret was already on the edge line', () => {
  const target = { value: 'visually wrapped draft', selectionStart: 18, selectionEnd: 18 }
  const up = composerHistoryKeyPlan({ key: 'ArrowUp', target }, {
    direction: 'older', value: target.value,
  }).probe
  assert.equal(composerHistoryProbeReachedBoundary(up, target), true, 'unchanged caret')

  target.selectionStart = target.selectionEnd = 6
  assert.equal(composerHistoryProbeReachedBoundary(up, target), false, 'moved up a visual line')

  target.selectionStart = target.selectionEnd = 0
  assert.equal(composerHistoryProbeReachedBoundary(up, target), false,
    'from the start of a wrapped line\'s second row, ↑ also lands at 0, so it proves nothing')

  target.selectionStart = target.selectionEnd = 18
  const upOneRow = composerHistoryKeyPlan({ key: 'ArrowUp', target }, {
    direction: 'older', value: target.value, linesWrap: false,
  }).probe
  target.selectionStart = target.selectionEnd = 0
  assert.equal(composerHistoryProbeReachedBoundary(upOneRow, target), true,
    'with no wrapped line, browsers move a top-row caret to the start: one ↑ switches')

  const end = target.value.length
  target.selectionStart = target.selectionEnd = 6
  const down = composerHistoryKeyPlan({ key: 'ArrowDown', target }, {
    direction: 'newer', value: target.value,
  }).probe
  target.selectionStart = target.selectionEnd = end
  assert.equal(composerHistoryProbeReachedBoundary(down, target), false,
    'a short wrapped last line can clamp ↓ to the end, so only an unchanged caret counts')
  target.selectionStart = target.selectionEnd = 6
  assert.equal(composerHistoryProbeReachedBoundary(down, target), true)

  target.value = 'edited after the key'
  assert.equal(composerHistoryProbeReachedBoundary(down, target), false, 'a stale probe never switches')
})

test('selection, modifier chords, and IME composition remain native', () => {
  assert.equal(composerHistoryKeyPlan(key('ArrowUp', {
    selectionStart: 0,
    selectionEnd: 2,
  }), { direction: 'older', value: 'draft' }), null)
  assert.equal(composerHistoryDirection(key('ArrowUp', { ctrlKey: true })), null)
  assert.equal(composerHistoryDirection(key('ArrowDown', { shiftKey: true })), null)
  assert.equal(composerHistoryDirection(key('ArrowUp', { isComposing: true })), null)
  assert.equal(composerHistoryDirection(key('ArrowUp')), 'older')
  assert.equal(composerHistoryDirection(key('ArrowDown')), 'newer')
})
