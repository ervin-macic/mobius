/* Keep a grant's workspace in this tab without touching owner workspace keys. */
export function isSharedBrowserRoute(pathname = globalThis.location?.pathname) {
  const path = sharedBrowserRoutePath()
  return pathname === path || pathname === `${path}/`
}

export function sharedBrowserRoutePath() {
  const base = (import.meta.env?.BASE_URL || '/').replace(/\/$/, '')
  return `${base}/shell/shared`
}

// A few legacy full-document links intentionally leave React navigation. Keep
// only links into this origin's shell in the guest route; external URLs and
// unrelated application paths must retain their original destination.
export function sharedBrowserShellHref(href, currentLocation = globalThis.location) {
  if (!isSharedBrowserRoute(currentLocation?.pathname)) return href
  try {
    const url = new URL(href, currentLocation.href)
    const currentUrl = new URL(currentLocation.href)
    const shellPath = sharedBrowserRoutePath().replace(/\/shared$/, '')
    if (url.origin !== currentUrl.origin
      || (url.pathname !== shellPath && url.pathname !== `${shellPath}/`)) return href
    url.pathname = sharedBrowserRoutePath()
    return url.href
  } catch { return href }
}

let cacheBusterSequence = 0
export function sharedBrowserCacheBuster() {
  cacheBusterSequence += 1
  return `${Date.now().toString(36)}-${cacheBusterSequence.toString(36)}-${globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2)}`
}

const memoryValues = new Map()
const memoryStorage = {
  get length() { return memoryValues.size },
  key: index => [...memoryValues.keys()][index] ?? null,
  getItem: key => memoryValues.get(key) ?? null,
  setItem: (key, value) => memoryValues.set(key, String(value)),
  removeItem: key => memoryValues.delete(key),
}

export function sharedBrowserStorageForGrant(grantId) {
  let storage = memoryStorage
  try { storage = globalThis.sessionStorage || memoryStorage } catch { /* memory-only */ }
  return sharedBrowserWorkspaceStorage(grantId, storage)
}

export function sharedBrowserWorkspaceStorage(grantId, storage) {
  const prefix = `mobius:shared-browser:${encodeURIComponent(grantId)}:`
  const keys = () => Array.from({ length: storage.length }, (_, index) => storage.key(index))
    .filter(key => key?.startsWith(prefix))
    .map(key => key.slice(prefix.length))
  return {
    getItem: key => storage.getItem(prefix + key),
    setItem: (key, value) => storage.setItem(prefix + key, value),
    removeItem: key => storage.removeItem(prefix + key),
    key: index => keys()[index] ?? null,
    get length() { return keys().length },
  }
}

// The active grant's storage is one stable object, so a module that asks for
// both durable and tab storage sees a single guest store rather than two views
// of the same keys.
let activeGrantId = null
let activeGrantStorage = null
export function setActiveSharedBrowserGrantId(grantId) {
  const next = grantId == null ? null : String(grantId)
  // Renewal re-announces the same grant; keep its storage object stable.
  if (next === activeGrantId) return
  activeGrantId = next
  activeGrantStorage = next == null ? null : sharedBrowserStorageForGrant(next)
}

export function currentSharedBrowserStorage() {
  return isSharedBrowserRoute() ? activeGrantStorage : null
}
