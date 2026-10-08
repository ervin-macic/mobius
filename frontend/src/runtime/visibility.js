// window.mobius.visible / onVisibilityChange — whether this app is on screen.
//
// The shell keeps recently used app frames mounted but hidden (see
// Shell/appFrameCache.js). Hiding them with `visibility:hidden` on a shell
// ancestor does NOT change the frame's own `document.hidden`, which only flips
// when the whole browser tab is hidden. AppCanvas therefore posts
// `moebius:frame-visibility` to every frame on load and on each foreground
// change. This module combines the two signals: the app is visible only when
// the shell says its frame is foreground AND the document itself is not hidden.
// Apps use it to pause polling, animation, and audio while out of sight.
//
// Only the hosting shell window may set the verdict (`event.source ===
// window.parent`, matching immersive.js and navigation.js). Until the shell's
// first verdict arrives the frame is assumed foreground, so an app mounted
// outside AppCanvas keeps document semantics.

export function makeVisibility({ win, doc } = {}) {
  // The frame bootstrap retains verdicts sent before this bundle evaluates.
  let frameVisible = typeof win?.__mobiusFrameVisible === 'boolean'
    ? win.__mobiusFrameVisible : true
  let visible = frameVisible && !doc?.hidden
  const listeners = new Set()

  function recompute() {
    const next = frameVisible && !doc?.hidden
    if (next === visible) return
    visible = next
    for (const cb of [...listeners]) {
      try { cb(next) } catch (e) {}
    }
  }

  if (win && win.parent && win.parent !== win) {
    win.addEventListener('message', (e) => {
      if (e.source !== win.parent) return
      const msg = e.data
      if (!msg || msg.type !== 'moebius:frame-visibility') return
      if (typeof msg.visible !== 'boolean') return
      frameVisible = msg.visible
      recompute()
    })
  }
  doc?.addEventListener?.('visibilitychange', recompute)

  return {
    get visible() { return visible },
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
