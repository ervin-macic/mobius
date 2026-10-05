import { useRef, useState } from 'react'
import { sessionStore } from '../../lib/workspaceStorage.js'


// Disclosure state is screen state, not transcript data. Keep it for the
// browser session so leaving a chat and returning restores the exact activity,
// thought, and tool rows the reader opened without writing presentation state
// into the durable conversation.
const STORAGE_PREFIX = 'chat-disclosures:'
// The memory cache mirrors exactly one store. A different store (another
// shared grant) starts from that store's own contents.
const cache = new Map()
let cachedStore = null

function disclosureCache(store) {
  if (store !== cachedStore) {
    cache.clear()
    cachedStore = store
  }
  return cache
}

function storageKey(chatId) {
  return `${STORAGE_PREFIX}${chatId}`
}

function readOpenKeys(chatId, store = sessionStore()) {
  const id = String(chatId || '')
  if (!id) return new Set()
  const openKeysByChat = disclosureCache(store)
  if (openKeysByChat.has(id)) return openKeysByChat.get(id)
  let keys = []
  try {
    const parsed = JSON.parse(store?.getItem(storageKey(id)) || '[]')
    if (Array.isArray(parsed)) keys = parsed.filter(key => typeof key === 'string')
  } catch {}
  const openKeys = new Set(keys)
  openKeysByChat.set(id, openKeys)
  return openKeys
}

export function persistDisclosureOpen(chatId, disclosureKey, open) {
  const id = String(chatId || '')
  const key = String(disclosureKey || '')
  if (!id || !key) return
  const store = sessionStore()
  const openKeys = readOpenKeys(id, store)
  if (open) openKeys.add(key)
  else openKeys.delete(key)
  try {
    store?.setItem(storageKey(id), JSON.stringify([...openKeys]))
  } catch {}
}

export function disclosureIsOpen(chatId, disclosureKey) {
  return readOpenKeys(chatId).has(String(disclosureKey || ''))
}

export function useDisclosureState(chatId, disclosureKey) {
  const [open, setOpenState] = useState(
    () => disclosureIsOpen(chatId, disclosureKey),
  )
  const openRef = useRef(open)
  openRef.current = open
  const setOpen = (next) => {
    const value = typeof next === 'function' ? !!next(openRef.current) : !!next
    openRef.current = value
    persistDisclosureOpen(chatId, disclosureKey, value)
    setOpenState(value)
  }
  return [open, setOpen]
}

export function _resetDisclosureStateForTests() {
  cache.clear()
}
