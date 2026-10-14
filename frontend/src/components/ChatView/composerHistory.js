/**
 * Composer history: the owner's sent and queued messages as one list that the
 * chat box walks with ↑/↓, like a terminal.
 *
 * The list runs oldest to newest: sent messages, then queued ones, so the
 * first ↑ reaches the newest queued message. Entries are keyed by the
 * message's stable `cid`, which a queued message keeps when it starts, so an
 * edit follows its message instead of a shifting list position.
 *
 * The browse state is `{ key, draft, edits, openedAs, started }`. `key: null`
 * means the owner is editing their own draft. While browsing, `draft` holds
 * that set-aside draft until it is sent, and `edits` keeps per-entry changes
 * while the owner moves; Esc drops them, and an edit is also dropped once its
 * entry is saved or sent. Every operation is pure and returns the next state
 * with the value the composer should show, or null when there is nothing to do.
 */
import { isOwnerUserMessage } from './chatRuntimeState.js'
import { stripAugmentation } from './msgText.js'
import { cidOf } from './messageIdentity.js'

export const COMPOSER_DRAFT_STATE = Object.freeze({
  key: null,
  draft: '',
  edits: Object.freeze({}),
  openedAs: null,
  started: false,
})

/** Sent owner-authored messages as history entries, oldest first.
 * Attachment-only rows cannot be reconstructed by the composer, so they are
 * deliberately skipped. */
export function composerHistoryFromMessages(messages) {
  if (!Array.isArray(messages)) return []
  const entries = []
  messages.forEach((message, index) => {
    if (!isOwnerUserMessage(message)) return
    const text = typeof message?.content === 'string'
      ? stripAugmentation(message.content)
      : ''
    if (!text.trim()) return
    entries.push({
      key: cidOf(message) || `sent-${message.ts ?? index}`,
      kind: 'sent',
      text,
    })
  })
  return entries
}

/** Queued owner messages the composer can edit, in queue order. A rejected
 * outbox row keeps its exact text for Retry, so it is not editable. */
export function composerQueuedEntries(items) {
  if (!Array.isArray(items)) return []
  return items
    .filter(item => cidOf(item) && !item.recoveryAvailable)
    .map(item => ({
      key: cidOf(item),
      kind: 'queued',
      text: stripAugmentation(item.content || ''),
    }))
}

/** One list for ↑/↓. A row briefly present in both lists has already started,
 * so its transcript copy wins. */
export function composerHistoryEntries(sent = [], queued = []) {
  const sentKeys = new Set(sent.map(entry => entry.key))
  return [...sent, ...queued.filter(entry => !sentKeys.has(entry.key))]
}

function entryText(entry, edits) {
  return Object.hasOwn(edits, entry.key) ? edits[entry.key] : entry.text
}

function withoutEdit(edits, key) {
  if (!Object.hasOwn(edits, key)) return edits
  const next = { ...edits }
  delete next[key]
  return next
}

/** Remember what the box holds for the entry being left. Text equal to the
 * original is not an edit, so moving back to it never shows a stale overlay. */
function rememberCurrent(state, entries, value) {
  if (state.key == null) return state.edits
  const entry = entries.find(candidate => candidate.key === state.key)
  if (entry && value === entry.text) return withoutEdit(state.edits, state.key)
  return { ...state.edits, [state.key]: String(value) }
}

function showEntry(state, entries, index, edits) {
  const entry = entries[index]
  return {
    state: {
      key: entry.key,
      draft: state.draft,
      edits,
      openedAs: entry.kind,
      started: false,
    },
    value: entryText(entry, edits),
  }
}

/**
 * Step to an older (↑) or newer (↓) entry. The first ↑ sets the draft aside;
 * ↓ past the newest entry brings it back and keeps the edits for later.
 */
export function moveComposerHistory(state, entries, direction, value) {
  const list = Array.isArray(entries) ? entries : []
  if (direction === 'older') {
    if (list.length === 0) return null
    if (state.key == null) {
      return showEntry({ ...state, draft: String(value) }, list, list.length - 1, state.edits)
    }
    const index = list.findIndex(entry => entry.key === state.key)
    const next = index < 0 ? list.length - 1 : index - 1
    if (next < 0) return null
    return showEntry(state, list, next, rememberCurrent(state, list, value))
  }
  if (direction === 'newer') {
    if (state.key == null) return null
    const index = list.findIndex(entry => entry.key === state.key)
    if (index < 0 || index >= list.length - 1) {
      return setComposerEntryAside(state, list, value)
    }
    return showEntry(state, list, index + 1, rememberCurrent(state, list, value))
  }
  return null
}

/**
 * Bring the draft back but keep what the box holds as the open entry's edit,
 * as ↓ past the newest entry does. Cancelling the open queued message uses
 * this too: if the cancel does not go through, the message stays queued and
 * reopening it still shows the owner's unsaved edit.
 */
export function setComposerEntryAside(state, entries, value) {
  if (state.key == null) return null
  const list = Array.isArray(entries) ? entries : []
  return {
    state: { ...COMPOSER_DRAFT_STATE, edits: rememberCurrent(state, list, value) },
    value: state.draft,
  }
}

/** Open one entry directly (a queued message's pencil), setting the draft
 * aside exactly like the first ↑. */
export function openComposerHistoryEntry(state, entries, key, value) {
  const list = Array.isArray(entries) ? entries : []
  const index = list.findIndex(entry => entry.key === key)
  if (index < 0 || state.key === key) return null
  if (state.key == null) {
    return showEntry({ ...state, draft: String(value) }, list, index, state.edits)
  }
  return showEntry(state, list, index, rememberCurrent(state, list, value))
}

/**
 * Leave browsing and bring the draft back. Esc passes `discardEdits` and drops
 * every kept edit; after a save or send only that entry's edit is dropped.
 */
export function returnToComposerDraft(state, { discardEdits = false } = {}) {
  if (state.key == null) return null
  return {
    state: {
      ...COMPOSER_DRAFT_STATE,
      edits: discardEdits ? COMPOSER_DRAFT_STATE.edits : withoutEdit(state.edits, state.key),
    },
    value: state.draft,
  }
}

/**
 * What the chat box is showing, for its strip: a queued message (with its
 * place in the queue), an earlier sent message, or a queued message that
 * started while it was open. Null while the owner edits their draft.
 */
export function composerHistoryPosition(state, entries) {
  if (state.key == null) return null
  const list = Array.isArray(entries) ? entries : []
  const entry = list.find(candidate => candidate.key === state.key) || null
  const queued = list.filter(candidate => candidate.kind === 'queued')
  const queuedIndex = queued.findIndex(candidate => candidate.key === state.key)
  const started = state.started || (state.openedAs === 'queued' && queuedIndex < 0)
  return {
    kind: started ? 'started' : (entry?.kind || state.openedAs || 'sent'),
    entry,
    queuedNumber: queuedIndex + 1,
    queuedCount: queued.length,
    draft: state.draft,
  }
}

function hasModifier(event) {
  return !!(
    event?.altKey
    || event?.ctrlKey
    || event?.metaKey
    || event?.shiftKey
  )
}

function collapsedSelection(event) {
  const start = Number(event?.target?.selectionStart)
  const end = Number(event?.target?.selectionEnd)
  if (!Number.isInteger(start) || start !== end) return null
  return { start, end }
}

/** 'older' for a plain ↑, 'newer' for a plain ↓; modifier chords and IME
 * composition stay native. */
export function composerHistoryDirection(event) {
  if (event?.key !== 'ArrowUp' && event?.key !== 'ArrowDown') return null
  if (event.isComposing || event.nativeEvent?.isComposing || hasModifier(event)) {
    return null
  }
  return event.key === 'ArrowUp' ? 'older' : 'newer'
}

/**
 * Decide when a plain ↑/↓ may switch entries.
 *
 * An empty box has no caret movement to preserve, so it switches now. With
 * text, ↑/↓ belong to the caret until it is on the top (bottom) line. A source
 * newline above (below) the caret rules that out cheaply; otherwise visual
 * wrapping decides, which only the rendered textarea knows, so the key is
 * left to the browser and a probe checks where the caret landed.
 *
 * `linesWrap` says whether any line of the text wraps visually (only the
 * rendered textarea knows; see textareaHasWrappedLines). It defaults to true,
 * the conservative answer.
 *
 * Returns `{ when: 'now' }`, `{ when: 'after-native', probe }`, or null.
 */
export function composerHistoryKeyPlan(event, { direction, value = '', linesWrap = true } = {}) {
  const selection = collapsedSelection(event)
  if (!direction || !selection) return null
  const text = String(value)
  if (!text.length) return { when: 'now' }
  const lineEdge = direction === 'older'
    ? !text.slice(0, selection.start).includes('\n')
    : !text.slice(selection.end).includes('\n')
  if (!lineEdge) return null
  return {
    when: 'after-native',
    probe: {
      target: event.target,
      value: text,
      direction,
      ...selection,
      startMeansTop: direction === 'older' && !linesWrap,
    },
  }
}

/**
 * Whether the browser's own ↑/↓ showed the caret was already on the edge line.
 *
 * An unchanged caret proves it. Browsers also move a caret on the first line
 * to the very start, which saves a second ↑ — but only when no line wraps:
 * from the start of a wrapped first line's second row, ↑ also lands at 0
 * while the caret was not on the top line. The plan records that as
 * `startMeansTop`. ↓ is stricter still: landing at the end can also mean a
 * short wrapped last line clamped the caret, so only an unchanged caret
 * counts. Value and target checks discard stale probes.
 */
export function composerHistoryProbeReachedBoundary(probe, target) {
  if (
    !probe
    || !target
    || probe.target !== target
    || target.value !== probe.value
    || target.selectionStart !== target.selectionEnd
  ) {
    return false
  }
  if (target.selectionStart === probe.start) return true
  return probe.startMeansTop === true && target.selectionStart === 0
}
