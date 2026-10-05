import { useEffect, useRef } from 'react'

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  'summary',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

let bodyScrollLockCount = 0
let bodyOverflowBeforeLock = ''
const dialogStack = []

export function dialogFocusableElements(container) {
  return [...container.querySelectorAll(FOCUSABLE)]
    .filter(element => !element.hidden && element.getClientRects().length > 0)
}

/**
 * Find the branches that must be inert for a dialog with a local modality
 * boundary. The boundary itself stays live so sibling panes outside it are
 * never reached by the global modal contract.
 */
export function dialogSiblingElements(container, boundary = null) {
  const siblings = []
  const seen = new Set()
  const body = typeof document !== 'undefined' ? document.body : null
  let branch = container
  while (branch?.parentElement) {
    const parent = branch.parentElement
    for (const element of parent.children) {
      if (element !== branch && !seen.has(element)) {
        seen.add(element)
        siblings.push(element)
      }
    }
    if (parent === boundary || (body && parent === body)) break
    branch = parent
  }
  return siblings
}

// Nested dialogs can both make the same element inert. Each element is counted and gets its original
// state back only when the last dialog holding it lets go, whichever closes first.
const inertHolds = new WeakMap()

export function holdInert(element) {
  const hold = inertHolds.get(element) || { count: 0, original: element.inert }
  hold.count += 1
  inertHolds.set(element, hold)
  element.inert = true
}

export function releaseInert(element) {
  const hold = inertHolds.get(element)
  if (!hold) return
  hold.count -= 1
  if (hold.count > 0) return
  inertHolds.delete(element)
  element.inert = hold.original
}

function lockBodyScroll() {
  if (bodyScrollLockCount === 0) {
    bodyOverflowBeforeLock = document.body.style.overflow
    document.body.style.overflow = 'hidden'
  }
  bodyScrollLockCount += 1
}

function unlockBodyScroll() {
  bodyScrollLockCount = Math.max(0, bodyScrollLockCount - 1)
  if (bodyScrollLockCount === 0) {
    document.body.style.overflow = bodyOverflowBeforeLock
  }
}

/** Focus trap, Escape handling, restoration, and sibling inerting for dialogs. */
export default function useDialogFocus({
  open = true,
  containerRef,
  initialFocusRef,
  restoreFocusRef,
  shouldRestoreFocus,
  onClose,
  closeOnEscape = true,
  // Optional: return false to let an Escape press through without closing (for example while typing).
  shouldCloseOnEscape,
  modal = true,
  lockScroll = modal,
  // A local modal can block only its owning surface while leaving sibling
  // panes interactive. It deliberately does not trap focus or claim a
  // document-wide aria-modal barrier.
  inertBoundaryRef,
}) {
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const closeOnEscapeRef = useRef(closeOnEscape)
  closeOnEscapeRef.current = closeOnEscape
  const shouldCloseOnEscapeRef = useRef(shouldCloseOnEscape)
  shouldCloseOnEscapeRef.current = shouldCloseOnEscape

  useEffect(() => {
    if (!open) return undefined
    const container = containerRef.current
    if (!container) return undefined
    let active = true
    const stackEntry = { container }
    dialogStack.push(stackEntry)
    const previouslyFocused = document.activeElement
    const explicitRestoreTarget = restoreFocusRef?.current
    if (lockScroll) lockBodyScroll()

    // Modal dialogs are rendered in place rather than through a body portal.
    // A normal modal inerts sibling branches all the way to body. A local
    // modal stops at its explicit boundary, so only the owning surface is
    // blocked and sibling workspace panes remain interactive.
    const boundary = inertBoundaryRef?.current
    const siblings = []
    if (modal) {
      dialogSiblingElements(container).forEach(element => {
        siblings.push(element)
        holdInert(element)
      })
    } else if (boundary) {
      dialogSiblingElements(container, boundary).forEach(element => {
        siblings.push(element)
        holdInert(element)
      })
    }

    const focusInitial = () => {
      if (
        !active
        || !container.isConnected
        || dialogStack.at(-1) !== stackEntry
      ) return
      const target = initialFocusRef?.current
        || dialogFocusableElements(container)[0]
        || container
      target?.focus?.({ preventScroll: true })
    }
    queueMicrotask(focusInitial)

    function onKeyDown(event) {
      // Only the topmost active dialog owns Escape and Tab. Without this gate,
      // nested/sibling dialogs can both close or both redirect focus from one
      // keypress even though inerting correctly hides the lower surface.
      if (dialogStack.at(-1) !== stackEntry) return
      const eventIsInsideDialog = container.contains(event.target)
      if (
        event.key === 'Escape'
        && closeOnEscapeRef.current
        && (modal || eventIsInsideDialog)
        && shouldCloseOnEscapeRef.current?.(event) !== false
      ) {
        event.preventDefault()
        onCloseRef.current?.()
        return
      }
      if (event.key !== 'Tab' || !modal) return
      const focusable = dialogFocusableElements(container)
      if (focusable.length === 0) {
        event.preventDefault()
        container.focus?.()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      active = false
      document.removeEventListener('keydown', onKeyDown, true)
      const stackIndex = dialogStack.lastIndexOf(stackEntry)
      if (stackIndex !== -1) dialogStack.splice(stackIndex, 1)
      siblings.forEach(releaseInert)
      if (lockScroll) unlockBodyScroll()
      if (shouldRestoreFocus?.() === false) return
      if (explicitRestoreTarget) {
        explicitRestoreTarget.focus?.({ preventScroll: true })
      } else {
        previouslyFocused?.focus?.({ preventScroll: true })
      }
    }
  }, [open, containerRef, initialFocusRef, restoreFocusRef, shouldRestoreFocus, lockScroll, modal, inertBoundaryRef])
}
