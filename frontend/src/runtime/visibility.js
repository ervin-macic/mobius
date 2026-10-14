// window.mobius.visible / onVisibilityChange — whether this app is on screen.
//
// The shell hides inactive app frames with `visibility:hidden` on an ancestor,
// which does not change the frame's own `document.hidden`, so AppCanvas posts
// `moebius:frame-visibility` instead. The app is visible only while the shell
// shows its frame AND the document is not hidden. Only the hosting window may
// set the verdict. Its first verdict usually lands before this runtime runs,
// so app-frame.html keeps it and init() seeds it through `setFrameVisible`.
// Without any verdict the frame counts as foreground (plain document
// semantics for hosts other than AppCanvas).

export function makeVisibility({ win, doc } = {}) {
  let frameVisible = true
  let visible = !doc?.hidden
  const listeners = new Set()

  function recompute() {
    const next = frameVisible && !doc?.hidden
    if (next === visible) return
    visible = next
    for (const cb of [...listeners]) {
      try { cb(next) } catch (e) {}
    }
  }

  function setFrameVisible(next) {
    if (typeof next !== 'boolean') return
    frameVisible = next
    recompute()
  }

  if (win && win.parent && win.parent !== win) {
    win.addEventListener('message', (e) => {
      if (e.source !== win.parent) return
      const msg = e.data
      if (!msg || msg.type !== 'moebius:frame-visibility') return
      setFrameVisible(msg.visible)
    })
  }
  doc?.addEventListener?.('visibilitychange', recompute)

  return {
    get visible() { return visible },
    setFrameVisible,
    // cb(boolean) fires immediately with the current value and again whenever
    // it changes. Returns an unsubscribe function.
    onVisibilityChange(cb) {
      if (typeof cb !== 'function') return () => {}
      listeners.add(cb)
      try { cb(visible) } catch (e) {}
      return () => { listeners.delete(cb) }
    },
  }
}
