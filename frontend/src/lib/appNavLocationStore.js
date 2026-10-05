import { validNavLocationText } from './appNavLocation.js'
import { sessionStore } from './workspaceStorage.js'

// Where each app frame last reported it was, kept by the shell for the frame
// that replaces it (code update, eviction remount, crash reload, or a shell
// reload in this tab). Tab-scoped workspace storage: it survives a reload but
// not the tab, so two shell tabs never overwrite each other's place, and a
// guest's grant storage keeps it out of the owner's. Each entry is bound to
// the app's installation nonce, so a reused id or a data wipe starts fresh.

const KEY_PREFIX = 'mobius:app-nav-location:'

function keyFor(appId) {
  return `${KEY_PREFIX}${String(appId)}`
}

export function readAppNavLocation(appId, instanceId, store = sessionStore()) {
  try {
    const entry = JSON.parse(store?.getItem(keyFor(appId)) || 'null')
    if (!entry || entry.instance !== (instanceId || null)) return null
    return validNavLocationText(entry.location)
  } catch {
    return null
  }
}

/** Store validated location text; anything else clears the entry. */
export function writeAppNavLocation(appId, instanceId, text, store = sessionStore()) {
  const location = validNavLocationText(text)
  try {
    if (location === null) {
      store?.removeItem(keyFor(appId))
      return
    }
    store?.setItem(keyFor(appId), JSON.stringify({
      instance: instanceId || null,
      location,
    }))
  } catch {
    // Storage full or unavailable: the app simply reopens at its start.
  }
}

export function clearAppNavLocations(store = sessionStore()) {
  try {
    for (let index = (store?.length || 0) - 1; index >= 0; index -= 1) {
      const key = store.key(index)
      if (key?.startsWith(KEY_PREFIX)) store.removeItem(key)
    }
  } catch {}
}
