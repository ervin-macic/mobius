/**
 * Host-painted video tiles for `media.call`.
 *
 * The shell paints call video over the live app frame at rectangles the app
 * supplies in its own CSS pixels. The container is a non-interactive layer
 * covering exactly the frame's box inside `.canvas-wrap`, so tiles move and
 * resize with the frame and are clipped to it. Only validated numbers reach
 * style properties, and the app never receives these elements or the
 * MediaStreams they show. Updates mutate styles in place, so an app may move
 * tiles every animation frame without re-rendering the shell.
 */
export function createCallTileLayer({ getContainer }) {
  let painted = new Map()

  function release(entry) {
    try { entry.video.pause?.() } catch { /* already detached */ }
    entry.video.srcObject = null
    entry.element.remove()
  }

  function create(container) {
    const document = container.ownerDocument
    const element = document.createElement('div')
    element.className = 'canvas-call-tile'
    Object.assign(element.style, {
      position: 'absolute',
      overflow: 'hidden',
      pointerEvents: 'none',
    })
    const video = document.createElement('video')
    // Audio plays through the call's Web Audio graph, never the tile.
    video.muted = true
    video.autoplay = true
    video.playsInline = true
    video.disablePictureInPicture = true
    video.setAttribute('playsinline', '')
    Object.assign(video.style, {
      display: 'block',
      width: '100%',
      height: '100%',
      objectFit: 'cover',
      pointerEvents: 'none',
    })
    element.appendChild(video)
    container.appendChild(element)
    return { element, video, stream: null }
  }

  function place(entry, tile, order) {
    Object.assign(entry.element.style, {
      left: `${tile.x}px`,
      top: `${tile.y}px`,
      width: `${tile.width}px`,
      height: `${tile.height}px`,
      borderRadius: `${tile.radius}px`,
      opacity: String(tile.opacity),
      // Later tiles in the app's list paint above earlier ones.
      zIndex: String(order + 1),
    })
    entry.video.style.transform = tile.mirror ? 'scaleX(-1)' : ''
    if (entry.stream !== tile.stream) {
      entry.stream = tile.stream
      entry.video.srcObject = tile.stream
      try { entry.video.play?.()?.catch?.(() => {}) } catch { /* autoplay retries on data */ }
    }
  }

  return {
    // `tiles` is the complete validated list, each with the shell-owned
    // `stream` to show. Anything not listed is removed.
    paint(tiles) {
      const container = getContainer?.() || null
      const next = new Map()
      if (container) {
        const occurrences = new Map()
        tiles.forEach((tile, order) => {
          const count = occurrences.get(tile.peer) || 0
          occurrences.set(tile.peer, count + 1)
          const key = `${tile.peer}\u0000${count}`
          let entry = painted.get(key)
          if (entry && entry.element.parentNode !== container) entry = null
          entry ||= create(container)
          place(entry, tile, order)
          next.set(key, entry)
        })
      }
      for (const [key, entry] of painted) {
        if (next.get(key) !== entry) release(entry)
      }
      painted = next
    },
    destroy() {
      for (const entry of painted.values()) release(entry)
      painted = new Map()
    },
  }
}
