import { useCallback, useEffect, useState } from 'react'
import {
  managedAppEventForShellEvent,
  managedAppFrameMessage,
} from '../lib/managedAppEvents.js'

// The shell's latest app lifecycle event, sequenced so a repeated update of
// the same app still changes state. `observe` takes every system event.
export function useManagedAppEvent() {
  const [event, setEvent] = useState(null)
  const observe = useCallback((ev) => {
    if (ev?.type !== 'app_updated') return
    setEvent(current => managedAppEventForShellEvent(
      ev, current?.sequence || 0,
    ))
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
  useEffect(() => {
    const message = managedAppFrameMessage(event, capabilityContract)
    if (!message) return
    for (const frame of framesRef.current.values()) {
      frame?.contentWindow?.postMessage(message, '*')
    }
  }, [capabilityContract, event, framesRef])
}
