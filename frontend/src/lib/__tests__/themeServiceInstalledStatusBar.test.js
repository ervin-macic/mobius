import { test } from 'node:test'
import assert from 'node:assert/strict'

import { reloadForInstalledStatusBar } from '../themeService.js'
import { consumeReturnView } from '../navigationPersistence.js'

function fakeStorage() {
  const values = new Map()
  return {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
}

function fakeLocation() {
  return { reloads: 0, reload() { this.reloads += 1 } }
}

test('an installed iPhone app reloads into Settings so its status bar adopts the new theme', () => {
  const location = fakeLocation()
  const storage = fakeStorage()
  assert.equal(reloadForInstalledStatusBar({ navigator: { standalone: true }, location, storage }), true)
  assert.equal(location.reloads, 1)
  assert.deepEqual(consumeReturnView(storage), { view: 'settings' })
})

test('browser tabs and other platforms keep the live theme switch without reloading', () => {
  for (const navigator of [{}, { standalone: false }, undefined]) {
    const location = fakeLocation()
    const storage = fakeStorage()
    assert.equal(reloadForInstalledStatusBar({ navigator, location, storage }), false)
    assert.equal(location.reloads, 0)
    assert.equal(consumeReturnView(storage), null)
  }
})
