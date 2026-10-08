import assert from 'node:assert/strict'
import { test } from 'node:test'

import { makeVisibility } from '../../runtime/visibility.js'

function fakeFrame({ hidden = false } = {}) {
  const parent = { name: 'shell' }
  const winListeners = []
  const docListeners = []
  const win = {
    parent,
    addEventListener(type, cb) { if (type === 'message') winListeners.push(cb) },
  }
  const doc = {
    hidden,
    addEventListener(type, cb) { if (type === 'visibilitychange') docListeners.push(cb) },
  }
  return {
    win,
    doc,
    parent,
    post(data, source = parent) { for (const cb of winListeners) cb({ source, data }) },
    setDocumentHidden(next) {
      doc.hidden = next
      for (const cb of docListeners) cb()
    },
  }
}

test('a frame the shell hides reports invisible even while the tab is visible', () => {
  const frame = fakeFrame()
  const visibility = makeVisibility(frame)
  const seen = []
  visibility.onVisibilityChange((v) => seen.push(v))

  frame.post({ type: 'moebius:frame-visibility', visible: false })
  assert.equal(visibility.visible, false)
  frame.post({ type: 'moebius:frame-visibility', visible: true })
  assert.equal(visibility.visible, true)
  assert.deepEqual(seen, [true, false, true])
})

test('a hidden document keeps the app invisible whatever the shell says', () => {
  const frame = fakeFrame()
  const visibility = makeVisibility(frame)
  const seen = []
  visibility.onVisibilityChange((v) => seen.push(v))

  frame.setDocumentHidden(true)
  frame.post({ type: 'moebius:frame-visibility', visible: true })
  assert.equal(visibility.visible, false)
  frame.setDocumentHidden(false)
  assert.equal(visibility.visible, true)
  // Shell hide while the tab is hidden does not double-notify.
  frame.setDocumentHidden(true)
  frame.post({ type: 'moebius:frame-visibility', visible: false })
  frame.setDocumentHidden(false)
  assert.equal(visibility.visible, false)
  assert.deepEqual(seen, [true, false, true, false])
})

test('frame visibility ignores messages that do not come from the parent shell', () => {
  const frame = fakeFrame()
  const visibility = makeVisibility(frame)
  frame.post({ type: 'moebius:frame-visibility', visible: false }, { name: 'nested child' })
  frame.post({ type: 'moebius:frame-visibility', visible: 'no' })
  assert.equal(visibility.visible, true)
})

test('an unsubscribed visibility listener stops receiving changes', () => {
  const frame = fakeFrame({ hidden: true })
  const visibility = makeVisibility(frame)
  const seen = []
  const unsubscribe = visibility.onVisibilityChange((v) => seen.push(v))
  unsubscribe()
  frame.setDocumentHidden(false)
  assert.deepEqual(seen, [false])
  assert.equal(visibility.visible, true)
})

test('a verdict retained before runtime startup seeds the first callback', () => {
  const frame = fakeFrame()
  frame.win.__mobiusFrameVisible = false
  const visibility = makeVisibility(frame)
  const seen = []
  visibility.onVisibilityChange(v => seen.push(v))
  assert.equal(visibility.visible, false)
  assert.deepEqual(seen, [false])
  frame.post({ type: 'moebius:frame-visibility', visible: true })
  assert.deepEqual(seen, [false, true])
})

test('only boolean retained verdicts override the foreground default', () => {
  for (const verdict of [undefined, null, 'false', 0, true]) {
    const frame = fakeFrame()
    frame.win.__mobiusFrameVisible = verdict
    assert.equal(makeVisibility(frame).visible, true)
  }
  const hidden = fakeFrame({ hidden: true })
  hidden.win.__mobiusFrameVisible = true
  assert.equal(makeVisibility(hidden).visible, false)
})
