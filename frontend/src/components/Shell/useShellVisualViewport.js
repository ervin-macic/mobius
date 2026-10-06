/* Keep the full-screen shell inside the actually visible mobile viewport. */

import { useLayoutEffect } from 'react'
import {
  captureLayoutSpace,
  clientLengthToLayout,
} from '../../lib/layoutSpace.js'

// Ignore small browser-bar changes; a software keyboard consumes much more.
const MIN_KEYBOARD_INSET = 80

function clearShellFrame(root) {
  root.style.removeProperty('top')
  root.style.removeProperty('bottom')
  root.style.removeProperty('height')
  root.style.removeProperty('--shell-safe-bottom-inset')
}

/**
 * Fit the shell above a keyboard, or to installed WebKit's drawable viewport.
 * Removing the previous inline frame first makes the shell's CSS layout the
 * baseline, so the next opening cannot reuse a stale window-height reading.
 */
export function fitShellToVisualViewport(root, viewport, { fitViewport = false, safeBottomInset = 0 } = {}) {
  if (!root) return false
  clearShellFrame(root)

  const space = captureLayoutSpace(root)
  const visibleClientHeight = Number(viewport?.height)
  if (!(visibleClientHeight > 0)) return false
  const visibleHeight = clientLengthToLayout(visibleClientHeight, space)
  const layoutHeight = space.height
  const coveredHeight = layoutHeight - visibleHeight
  const keyboardOpen = coveredHeight >= clientLengthToLayout(MIN_KEYBOARD_INSET, space)
  if (!keyboardOpen && !fitViewport) return false
  if (coveredHeight < 0) return false

  const visibleTop = Math.min(
    coveredHeight,
    Math.max(0, clientLengthToLayout(Number(viewport.offsetTop) || 0, space)),
  )
  root.style.setProperty('top', `${visibleTop}px`)
  root.style.setProperty('bottom', 'auto')
  root.style.setProperty('height', `${visibleHeight}px`)
  // The visual viewport ends above the keyboard. iOS can still report its
  // Home-indicator safe area, but that area is now covered by the keyboard.
  // A closed-keyboard viewport may already exclude part or all of the unsafe
  // bottom band. Reserve only the part still inside the fitted shell.
  const clippedBottom = Math.max(0, coveredHeight - visibleTop)
  const remainingInset = keyboardOpen ? 0 : Math.max(0, safeBottomInset - clippedBottom)
  root.style.setProperty('--shell-safe-bottom-inset', `${remainingInset}px`)
  return true
}

/**
 * Read the installed-display contract the stylesheet publishes on the shell.
 * `--shell-bottom-inset-reserve` is the share of the device's bottom safe area
 * the composer keeps clear (1 when the platform does not set one).
 */
export function installedViewportOptions(style) {
  const read = name => style.getPropertyValue(name).trim()
  const inset = Number.parseFloat(read('--shell-device-bottom-inset')) || 0
  const reserve = Number.parseFloat(read('--shell-bottom-inset-reserve'))
  const share = Number.isFinite(reserve) ? Math.min(1, Math.max(0, reserve)) : 1
  return {
    fitViewport: read('--shell-fit-visual-viewport') === '1',
    safeBottomInset: inset * share,
  }
}

export default function useShellVisualViewport(rootRef) {
  useLayoutEffect(() => {
    const viewport = window.visualViewport
    const root = rootRef.current
    if (!viewport || !root) return undefined

    let frameRequest = 0
    const apply = () => {
      frameRequest = 0
      fitShellToVisualViewport(root, viewport, installedViewportOptions(getComputedStyle(root)))
    }
    const applySoon = () => {
      if (!frameRequest) frameRequest = requestAnimationFrame(apply)
    }

    apply()
    viewport.addEventListener('resize', applySoon)
    viewport.addEventListener('scroll', applySoon)
    window.addEventListener('resize', applySoon)
    window.addEventListener('pageshow', applySoon)

    return () => {
      if (frameRequest) cancelAnimationFrame(frameRequest)
      viewport.removeEventListener('resize', applySoon)
      viewport.removeEventListener('scroll', applySoon)
      window.removeEventListener('resize', applySoon)
      window.removeEventListener('pageshow', applySoon)
      clearShellFrame(root)
    }
  }, [rootRef])
}
