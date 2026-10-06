import { useCallback, useEffect, useRef, useState } from 'react'

// The shell's latest app lifecycle event, sequenced so a repeated update of
// the same app still changes state. `observe` takes every system event.
export function useManagedAppEvent() {
  const [event, setEvent] = useState(null)
  const observe = useCallback((ev) => {
    if (ev?.type !== 'app_updated') return
    setEvent(current => ({
      type: 'app_updated',
      appId: ev.appId == null ? null : String(ev.appId),
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
  const delivered = useRef(new WeakMap())
  useEffect(() => {
    if (event?.type !== 'app_updated'
      || capabilityContract?.data?.manage_apps !== true) return
    const message = { type: 'moebius:managed-app-event', event }
    for (const frame of framesRef.current.values()) {
      if (!frame?.contentWindow || delivered.current.get(frame) === event.sequence) continue
      frame.contentWindow.postMessage(message, '*')
      delivered.current.set(frame, event.sequence)
    }
  }, [capabilityContract, event, framesRef])
}
