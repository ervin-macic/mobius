import {
  SHELL_COMMAND_DEFINITIONS,
  normalizeShortcutBinding,
  shortcutMatches,
} from './keyboardShortcuts.js'

// The parent is the app's own document, where app code runs and can forge the
// advertisement. Accept only what the shell itself could advertise: a command
// it captures in app frames, bound to a Cmd/Ctrl chord (owner overrides are
// held to the same rule), so ordinary typing never leaves this document.
const CAPTURABLE_ACTIONS = new Set(
  SHELL_COMMAND_DEFINITIONS.filter(command => command.captureInMiniApps).map(command => command.id),
)

function shellShortcut(item) {
  const binding = normalizeShortcutBinding(item?.binding)
  return binding?.mod && CAPTURABLE_ACTIONS.has(item.actionId)
    ? { actionId: item.actionId, binding }
    : null
}

/*
 * Shell shortcuts for a document nested inside an app frame, such as the
 * embedded agent chat. The browser delivers a key only to the focused
 * document, so this document captures the chords its parent advertises
 * (`moebius:frame-shortcuts`), limited to real shell commands on Cmd/Ctrl
 * chords, and forwards the named action
 * (`moebius:shell-shortcut`). The app frame relays it to the shell, which
 * accepts only actions it advertised. An app that opted out advertises none.
 */
export function installFrameShellShortcuts(win = window) {
  const parent = win.parent
  if (!parent || parent === win) return () => {}
  let shortcuts = []

  const onMessage = (event) => {
    if (event.source !== parent || event.data?.type !== 'moebius:frame-shortcuts') return
    shortcuts = Array.isArray(event.data.shortcuts)
      ? event.data.shortcuts.map(shellShortcut).filter(Boolean)
      : []
  }
  const onKeyDown = (event) => {
    const shortcut = shortcuts.find(item => shortcutMatches(event, item.binding))
    if (!shortcut) return
    event.preventDefault()
    event.stopImmediatePropagation()
    parent.postMessage({ type: 'moebius:shell-shortcut', actionId: shortcut.actionId }, '*')
  }

  win.addEventListener('message', onMessage)
  win.document.addEventListener('keydown', onKeyDown, true)
  parent.postMessage({ type: 'moebius:frame-shortcuts-request' }, '*')
  return () => {
    win.removeEventListener('message', onMessage)
    win.document.removeEventListener('keydown', onKeyDown, true)
  }
}
