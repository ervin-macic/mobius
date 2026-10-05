/* Install for the guide's app cards. Every app goes through the same two steps: its access is
   checked (read-only), the owner reviews it in the confirmation, and Confirm and install installs
   exactly what was reviewed, bound to its digest as in App Store. The shell refreshes its own app
   list after an install, so the card turns to Installed on its own.

   One app is handled at a time, so the access list the owner reads is always the one they confirm:
   other Install buttons wait while a check, a review or an install is open, and a check that
   returns after the review was dismissed (or the guide was left) is dropped. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { capabilityRows, installReviewedApp, previewAppAccess } from './walkthroughAccess.js'

const CHANGED_NOTICE = 'The publisher changed this app’s access after you started. Nothing was installed. Check the access below.'

function reviewOf(id, item, preview, notice = '') {
  return { id, item, rows: capabilityRows(preview.capability_contract), manifest: preview.manifest || null, digest: preview.capability_digest, notice }
}

export function useAppInstall(catalog) {
  const [status, setStatus] = useState({})
  const [confirmation, setConfirmation] = useState(null)
  // Bumped whenever a review is dismissed or the hook unmounts: results of older requests are ignored.
  const generation = useRef(0)
  const working = useRef(false)
  useEffect(() => () => { generation.current += 1 }, [])

  const setOne = useCallback((id, value) => setStatus(current => {
    const next = { ...current }
    if (value) next[id] = value
    else delete next[id]
    return next
  }), [])

  const begin = useCallback(async id => {
    const item = catalog?.get(id)
    if (!item?.manifest_url || working.current) return
    working.current = true
    const mine = ++generation.current
    setOne(id, { state: 'checking' })
    try {
      const preview = await previewAppAccess(item.manifest_url)
      if (mine !== generation.current) return
      setOne(id, null)
      setConfirmation(reviewOf(id, item, preview))
    } catch (error) {
      if (mine !== generation.current) return
      working.current = false
      setOne(id, { state: 'error', error: error.message || 'This app’s access could not be checked right now.' })
    }
  }, [catalog, setOne])

  const approve = useCallback(async () => {
    if (!confirmation) return
    const { id, item, digest } = confirmation
    const mine = generation.current
    setConfirmation(null)
    setOne(id, { state: 'installing' })
    try {
      const result = await installReviewedApp(item.manifest_url, digest)
      if (result.status === 'changed') {
        setOne(id, null)
        if (mine === generation.current) setConfirmation(reviewOf(id, item, result.preview, CHANGED_NOTICE))
        return
      }
      setOne(id, { state: 'installed', warnings: result.warnings })
    } catch (error) {
      setOne(id, { state: 'error', error: error.message || 'The app could not be installed.' })
    }
    if (mine === generation.current) working.current = false
  }, [confirmation, setOne])

  // Closing the review frees the guide for the next app and invalidates anything still on its way.
  const dismiss = useCallback(() => {
    generation.current += 1
    working.current = false
    setConfirmation(null)
    setStatus(current => Object.fromEntries(Object.entries(current).filter(([, value]) => value.state !== 'checking')))
  }, [])

  const statusOf = useCallback(id => status[id] || null, [status])
  const busy = confirmation != null || Object.values(status).some(value => value.state === 'checking' || value.state === 'installing')
  return { statusOf, begin, confirmation, approve, dismiss, busy }
}
