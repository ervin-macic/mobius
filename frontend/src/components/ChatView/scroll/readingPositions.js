import { localStore } from '../../../lib/workspaceStorage.js'

/**
 * Durable reading-position storage for chat scroll.
 *
 * This module owns only serialization and bounded retention. It deliberately
 * knows nothing about DOM geometry or live scroll modes: the controller turns
 * its current state into a durable anchor before calling `writeReadingPosition`.
 */

export const READING_POSITION_KEY = 'chat-reading-position'
const READING_POSITION_LIMIT = 300

// The memory copy mirrors exactly one store. A different store (another
// shared grant) starts from that store's own positions.
let cachedStore = null
let cachedPositions = null
function positions() {
  const store = localStore()
  if (cachedPositions && store === cachedStore) return cachedPositions
  cachedStore = store
  try {
    const parsed = JSON.parse(store?.getItem(READING_POSITION_KEY) || '{}')
    cachedPositions = (parsed && typeof parsed === 'object') ? parsed : {}
  }
  catch { cachedPositions = {} }
  return cachedPositions
}

// Logout is a terminal owner-session boundary. React/page lifecycle callbacks
// may still run before reload; disabling writes prevents those late callbacks
// from recreating data that logout just removed.
let writesEnabled = true

function persist() {
  if (!writesEnabled) return
  try {
    const entries = Object.entries(cachedPositions)
    if (entries.length > READING_POSITION_LIMIT) {
      const expired = entries
        .sort((a, b) => (b[1]?.at || 0) - (a[1]?.at || 0))
        .slice(READING_POSITION_LIMIT)
      for (const [chatId] of expired) delete cachedPositions[chatId]
    }
    cachedStore?.setItem(READING_POSITION_KEY, JSON.stringify(cachedPositions))
  }
  catch { /* best-effort position storage must never break scrolling */ }
}

export function readingPositionFor(chatId) {
  return positions()[String(chatId || '')] || null
}

export function hasReadingPosition(chatId) {
  return Object.hasOwn(positions(), String(chatId || ''))
}

export function writeReadingPosition(chatId, mode) {
  const id = String(chatId || '')
  const scopedPositions = positions()
  if (!id || !mode || mode.kind === 'INITIAL') {
    if (id) delete scopedPositions[id]
  } else {
    scopedPositions[id] = { ...mode, at: Date.now() }
  }
  persist()
}

export function forgetReadingPosition(chatId) {
  const id = String(chatId || '')
  const scopedPositions = positions()
  if (!(id in scopedPositions)) return false
  delete scopedPositions[id]
  persist()
  return true
}

/** The durable message row an activation needs before reveal. */
export function savedReadingAnchorKey(chatId) {
  const mode = readingPositionFor(chatId)
  return mode?.kind === 'ANCHOR_AT' && typeof mode.key === 'string'
    ? mode.key
    : null
}

/** Nested part paths need committed DOM validation before cache reveal. */
export function savedReadingAnchorHasNestedPart(chatId) {
  const mode = readingPositionFor(chatId)
  return mode?.kind === 'ANCHOR_AT'
    && Array.isArray(mode.part)
    && mode.part.length > 0
}

/** Replace one saved alias before restore consumes it. */
export function remapSavedReadingAnchor(chatId, fromKey, toKey) {
  const id = String(chatId || '')
  const scopedPositions = positions()
  const mode = scopedPositions[id]
  if (mode?.kind !== 'ANCHOR_AT'
      || mode.key !== fromKey
      || typeof toKey !== 'string'
      || !toKey) return false
  scopedPositions[id] = { ...mode, key: toKey, at: Date.now() }
  persist()
  return true
}

export const retireSavedReadingPosition = forgetReadingPosition

export function clearReadingPositions() {
  writesEnabled = false
  cachedPositions = {}
  cachedStore = localStore()
  try { cachedStore?.removeItem(READING_POSITION_KEY) } catch {}
}
