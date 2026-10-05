import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import {
  useManagedAppEvent,
  useManagedAppFrameForwarding,
} from '../useManagedAppEvents.js'

function fakeFrames() {
  const posted = []
  const frame = {
    contentWindow: {
      postMessage: (message, origin) => posted.push({ message, origin }),
    },
  }
  return { framesRef: { current: new Map([[0, frame]]) }, posted }
}

function renderShellAndFrame(framesRef, capabilityContract) {
  return renderHook((contract) => {
    const [event, observe] = useManagedAppEvent()
    useManagedAppFrameForwarding(framesRef, event, contract)
    return observe
  }, capabilityContract)
}

test('a finished app update reaches a manager frame in the Store shape', () => {
  const { framesRef, posted } = fakeFrames()
  const hook = renderShellAndFrame(framesRef, { data: { manage_apps: true } })
  assert.deepEqual(posted, [])

  hook.result.current({ type: 'theme_updated' })
  assert.deepEqual(posted, [])

  hook.result.current({ type: 'app_updated', appId: 42 })
  assert.deepEqual(posted, [{
    message: {
      type: 'moebius:managed-app-event',
      event: { type: 'app_updated', appId: '42', sequence: 1 },
    },
    origin: '*',
  }])

  // A second completion for the same app is delivered again.
  hook.result.current({ type: 'app_updated', appId: 42 })
  assert.equal(posted.length, 2)
  assert.equal(posted[1].message.event.sequence, 2)
  hook.unmount()
})

test('frames without manage_apps never receive app lifecycle events', () => {
  for (const contract of [null, { data: { manage_apps: false } }]) {
    const { framesRef, posted } = fakeFrames()
    const hook = renderShellAndFrame(framesRef, contract)
    hook.result.current({ type: 'app_updated', appId: 7 })
    assert.deepEqual(posted, [])
    hook.unmount()
  }
})
