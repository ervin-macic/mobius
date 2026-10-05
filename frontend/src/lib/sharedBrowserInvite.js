/* Consume shared-browser admission hints without persisting them or retaining URL fragments. */
export function consumeSharedBrowserEntry(location, history) {
  const fragment = location.hash?.replace(/^#/, '') || ''
  const params = new URLSearchParams(fragment)
  if (fragment) history.replaceState(history.state, '', location.pathname + location.search)
  const invitation = params.has('invite')
  const account = params.has('account-finalize')
  if (!invitation && !account) return null
  if (invitation && account) return { kind: 'invalid' }
  const key = invitation ? 'invite' : 'account-finalize'
  const value = params.get(key)
  if (!value || params.getAll(key).length !== 1) return { kind: 'invalid' }
  return { kind: invitation ? 'invite' : 'account', value }
}

export function watchSharedBrowserEntries(win, onEntry) {
  const admit = () => {
    const entry = consumeSharedBrowserEntry(win.location, win.history)
    if (entry) onEntry(entry)
  }
  win.addEventListener('hashchange', admit)
  return () => win.removeEventListener('hashchange', admit)
}
