/* The one place that decides which browser storage this page's workspace uses.
 *
 * The owner's shell uses the origin's localStorage and sessionStorage. A
 * shared-browser guest must never read or write owner state, so both
 * localStore() and sessionStore() return its active grant's tab-scoped
 * storage, or null before a grant is active. State that exists only for the
 * owner (and that a guest simply goes without) uses ownerStore() or
 * isOwnerWorkspace().
 */
import { currentSharedBrowserStorage, isSharedBrowserRoute } from './sharedBrowserWorkspace.js'

export function isOwnerWorkspace() {
  return !isSharedBrowserRoute()
}

/** Durable workspace state: owner localStorage, or the guest's grant storage. */
export function localStore() {
  if (!isOwnerWorkspace()) return currentSharedBrowserStorage()
  return ownerStore()
}

/** Tab workspace state: owner sessionStorage, or the guest's grant storage. */
export function sessionStore() {
  if (!isOwnerWorkspace()) return currentSharedBrowserStorage()
  try { return globalThis.sessionStorage ?? null } catch { return null }
}

/** Owner-only durable state; null for a guest. */
export function ownerStore() {
  if (!isOwnerWorkspace()) return null
  try { return globalThis.localStorage ?? null } catch { return null }
}
