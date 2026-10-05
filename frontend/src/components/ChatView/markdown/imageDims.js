/* Intrinsic layout metadata for chat Markdown images. */

const FRAME_MAX_WIDTH = 520
const FRAME_MIN_WIDTH = 120
const FRAME_CAP_H = 480
const FRAME_VIEWPORT_FRACTION = 0.6
const DEFAULT_VIEWPORT_H = 800

function mediaPathname(href) {
  if (!href || typeof href !== 'string') return null
  try {
    return new URL(href, 'https://mobius.local').pathname
  } catch {
    return null
  }
}

export function imageDimensionsForHref(href, mediaDimensions) {
  const pathname = mediaDimensions ? mediaPathname(href) : null
  const value = pathname ? mediaDimensions[pathname] : null
  if (!value || !Number.isInteger(value.width) || !Number.isInteger(value.height)) {
    return null
  }
  const { width, height } = value
  if (width <= 0 || height <= 0) return null
  return { width, height }
}

/**
 * True only when the server looked at this image and recorded it as
 * unreadable (an explicit null). A path missing from the map is unknown, not
 * broken: the map belongs to the saved text, while the displayed text can be
 * newer (live stream, promotion, joined steer replay).
 */
export function imageUnreadableForHref(href, mediaDimensions) {
  const pathname = mediaDimensions ? mediaPathname(href) : null
  return pathname != null && mediaDimensions[pathname] === null
}

/**
 * Builds the exact first-layout custom properties from server-read dimensions.
 *
 * @param {number} width
 * @param {number} height
 * @param {number} [viewportH]  visual-viewport height for the height cap
 * @returns {object|null}  a React style object, or null for invalid dims
 */
export function imageVarsFromDims(width, height, viewportH) {
  if (!(width > 0) || !(height > 0)) return null
  const ratio = width / height
  const vh = Number.isFinite(viewportH) && viewportH > 0
    ? viewportH
    : DEFAULT_VIEWPORT_H
  const cappedH = Math.min(vh * FRAME_VIEWPORT_FRACTION, FRAME_CAP_H)
  const fitWidth = Math.min(
    FRAME_MAX_WIDTH,
    Math.max(FRAME_MIN_WIDTH, Math.round(cappedH * ratio)),
  )
  return {
    '--md-image-ratio': `${width} / ${height}`,
    '--md-image-fit-width': `${fitWidth}px`,
  }
}
