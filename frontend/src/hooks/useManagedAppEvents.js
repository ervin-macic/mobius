import { useCallback, useEffect, useRef, useState } from 'react'

// The shell's latest app lifecycle event, sequenced so a repeated update of
// the same app still changes state. app_updated always carries an app id.
// `observe` takes every system event.
export function useManagedAppEvent() {
  const [event, setEvent] = useState(null)
  const observe = useCallback((ev) => {
    if (ev?.type !== 'app_updated' || ev.appId == null) return
    setEvent(current => ({
      type: 'app_updated',
      appId: String(ev.appId),
      sequence: (current?.sequence || 0) + 1,
    }))
  }, [])
  return [event, observe]
}

// App managers need the same lifecycle truth the shell already receives.
// Forward only the deliberately narrow app_updated projection, and only to
// frames whose reviewed contract grants manage_apps. This keeps the Store's
// update review in step when a resolver chat finishes while the Store
// iframe stays mounted.
export function useManagedAppFrameForwarding(
  framesRef, event, capabilityContract,
) {
  const lastSequence = useRef(event?.sequence || 0)
  useEffect(() => {
    if (event?.type !== 'app_updated' || event.sequence <= lastSequence.current) return
    // Consume once even if there is no eligible frame yet: new frames fetch
    // current state on mount, rather than replaying a stale lifecycle signal.
    lastSequence.current = event.sequence
    if (capabilityContract?.data?.manage_apps !== true) return
    const message = { type: 'moebius:managed-app-event', event }
    for (const frame of framesRef.current.values()) {
      if (!frame?.contentWindow) continue
      frame.contentWindow.postMessage(message, '*')
    }
  }, [capabilityContract, event, framesRef])
}
