import test from 'node:test'
import assert from 'node:assert/strict'

import { fitShellToVisualViewport, installedViewportOptions } from '../useShellVisualViewport.js'

function fakeShell(layoutHeight, zoom = 1) {
  const properties = new Map()
  return {
    layoutHeight,
    style: {
      setProperty: (name, value) => properties.set(name, value),
      removeProperty: name => properties.delete(name),
    },
    get clientHeight() {
      const framed = Number.parseFloat(properties.get('height'))
      return Number.isFinite(framed) ? framed : this.layoutHeight
    },
    offsetWidth: 1000,
    get offsetHeight() { return this.clientHeight },
    currentCSSZoom: zoom,
    getBoundingClientRect() {
      return {
        left: 0,
        top: 0,
        width: 1000 * zoom,
        height: this.clientHeight * zoom,
      }
    },
    property: name => properties.get(name),
  }
}

test('a keyboard overlay fits the shell to the visible viewport', () => {
  const root = fakeShell(860)
  assert.equal(fitShellToVisualViewport(root, {
    height: 492,
    offsetTop: 44,
  }), true)
  assert.equal(root.property('top'), '44px')
  assert.equal(root.property('bottom'), 'auto')
  assert.equal(root.property('height'), '492px')
  assert.equal(root.property('--shell-safe-bottom-inset'), '0px')
})

test('desktop author zoom is not mistaken for a software keyboard', () => {
  const root = fakeShell(1000, 0.9)
  assert.equal(fitShellToVisualViewport(root, {
    height: 900,
    offsetTop: 0,
  }), false)
  assert.equal(root.property('height'), undefined)
})

test('the keyboard threshold remains a painted-pixel policy under author zoom', () => {
  const root = fakeShell(1000, 0.9)
  assert.equal(fitShellToVisualViewport(root, {
    height: 821,
    offsetTop: 0,
  }), false)
  assert.equal(root.property('height'), undefined)
})

test('keyboard viewport dimensions cross into zoomed layout space once', () => {
  const root = fakeShell(1000, 0.9)
  assert.equal(fitShellToVisualViewport(root, {
    height: 540,
    offsetTop: 45,
  }), true)
  assert.equal(root.property('top'), '50px')
  assert.equal(root.property('height'), '600px')
})

test('open, close, and open again always remeasure the unframed shell', () => {
  const root = fakeShell(860)
  const keyboardOpen = { height: 492, offsetTop: 0 }

  assert.equal(fitShellToVisualViewport(root, keyboardOpen), true)
  assert.equal(root.property('height'), '492px')

  assert.equal(fitShellToVisualViewport(root, { height: 860, offsetTop: 0 }), false)
  assert.equal(root.property('height'), undefined)
  assert.equal(root.property('--shell-safe-bottom-inset'), undefined)

  assert.equal(fitShellToVisualViewport(root, keyboardOpen), true)
  assert.equal(root.property('height'), '492px')
})

test('ordinary layout resizing and small browser chrome keep the CSS frame', () => {
  const resizedRoot = fakeShell(492)
  assert.equal(fitShellToVisualViewport(resizedRoot, { height: 492 }), false)
  assert.equal(resizedRoot.property('height'), undefined)

  const chromeRoot = fakeShell(860)
  assert.equal(fitShellToVisualViewport(chromeRoot, { height: 781 }), false)
  assert.equal(chromeRoot.property('height'), undefined)
})

test('installed WebKit fits measured drawable heights and reserves only the remaining safe band', () => {
  for (const layoutHeight of [600, 900, 1200]) {
    for (const safeBottomInset of [0, 20, 40]) {
      for (const clippedBottom of [0, 10, 50]) {
        const root = fakeShell(layoutHeight)
        const height = layoutHeight - clippedBottom
        assert.equal(fitShellToVisualViewport(root, { height }, {
          fitViewport: true, safeBottomInset,
        }), true)
        assert.equal(root.property('height'), `${height}px`)
        assert.equal(root.property('--shell-safe-bottom-inset'), `${Math.max(0, safeBottomInset - clippedBottom)}px`)
      }
    }
  }
})

test('installed viewport inset accounts for top offset and author zoom', () => {
  const root = fakeShell(1000, 0.9)
  fitShellToVisualViewport(root, { height: 855, offsetTop: 27 }, {
    fitViewport: true, safeBottomInset: 30,
  })
  assert.equal(root.property('top'), '30px')
  assert.equal(root.property('height'), '950px')
  assert.equal(root.property('--shell-safe-bottom-inset'), '10px')
})

test('installed keyboard close and rotation remeasure both frame and safe area', () => {
  const root = fakeShell(1000)
  const installed = { fitViewport: true, safeBottomInset: 40 }
  fitShellToVisualViewport(root, { height: 980 }, installed)
  assert.equal(root.property('--shell-safe-bottom-inset'), '20px')
  fitShellToVisualViewport(root, { height: 600 }, installed)
  assert.equal(root.property('--shell-safe-bottom-inset'), '0px')
  fitShellToVisualViewport(root, { height: 980 }, installed)
  assert.equal(root.property('--shell-safe-bottom-inset'), '20px')
  root.layoutHeight = 600
  fitShellToVisualViewport(root, { height: 600 }, { ...installed, safeBottomInset: 0 })
  assert.equal(root.property('height'), '600px')
  assert.equal(root.property('--shell-safe-bottom-inset'), '0px')
})

test('missing viewport or leaving installed mode clears fitted geometry', () => {
  const root = fakeShell(1000)
  const installed = { fitViewport: true, safeBottomInset: 40 }
  for (const viewport of [undefined, { height: 0 }, { height: 1100 }]) {
    fitShellToVisualViewport(root, { height: 980 }, installed)
    assert.equal(fitShellToVisualViewport(root, viewport, installed), false)
    assert.equal(root.property('height'), undefined)
    assert.equal(root.property('--shell-safe-bottom-inset'), undefined)
  }
  fitShellToVisualViewport(root, { height: 980 }, installed)
  assert.equal(fitShellToVisualViewport(root, { height: 980 }), false)
  assert.equal(root.property('--shell-safe-bottom-inset'), undefined)
})

test('installed options scale the device bottom inset by the platform reserve', () => {
  const style = values => ({ getPropertyValue: name => values[name] ?? '' })
  assert.deepEqual(installedViewportOptions(style({
    '--shell-fit-visual-viewport': ' 1',
    '--shell-device-bottom-inset': ' 34px',
    '--shell-bottom-inset-reserve': ' 0.5',
  })), { fitViewport: true, safeBottomInset: 17 })
  assert.deepEqual(installedViewportOptions(style({
    '--shell-device-bottom-inset': '24px',
  })), { fitViewport: false, safeBottomInset: 24 }, 'no reserve keeps the whole inset')
  assert.equal(installedViewportOptions(style({
    '--shell-device-bottom-inset': '34px', '--shell-bottom-inset-reserve': '3',
  })).safeBottomInset, 34, 'the reserve never exceeds the safe area')
})
