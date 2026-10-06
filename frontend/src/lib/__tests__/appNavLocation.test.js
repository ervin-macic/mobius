import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  APP_NAV_LOCATION_MAX_BYTES,
  encodeNavLocation,
  validNavLocationText,
} from '../appNavLocation.js'
import {
  clearAppNavLocations,
  readAppNavLocation,
  writeAppNavLocation,
} from '../appNavLocationStore.js'

function memoryStore(initial = {}) {
  const items = new Map(Object.entries(initial))
  return {
    get length() { return items.size },
    key: index => [...items.keys()][index] ?? null,
    getItem: key => (items.has(key) ? items.get(key) : null),
    setItem: (key, value) => { items.set(key, String(value)) },
    removeItem: key => { items.delete(key) },
    items,
  }
}

test('locations stay per app and per installation', () => {
  const store = memoryStore()
  writeAppNavLocation(1, 'nonce-a', '{"view":"one"}', store)
  writeAppNavLocation(2, 'nonce-b', '{"view":"two"}', store)

  assert.equal(readAppNavLocation(1, 'nonce-a', store), '{"view":"one"}')
  assert.equal(readAppNavLocation(2, 'nonce-b', store), '{"view":"two"}')
  assert.equal(readAppNavLocation(3, 'nonce-a', store), null)
  assert.equal(readAppNavLocation(1, 'nonce-b', store), null, 'a reused id or wiped data starts fresh')
})

test('invalid or oversized reports clear the entry instead of being stored', () => {
  const store = memoryStore()
  writeAppNavLocation(1, null, '{"view":"one"}', store)
  writeAppNavLocation(1, null, 'alert(1)', store)
  assert.equal(readAppNavLocation(1, null, store), null)

  writeAppNavLocation(1, null, '{"view":"one"}', store)
  writeAppNavLocation(1, null, JSON.stringify({ q: 'x'.repeat(APP_NAV_LOCATION_MAX_BYTES) }), store)
  assert.equal(store.items.size, 0)

  writeAppNavLocation(1, null, '{"view":"one"}', store)
  writeAppNavLocation(1, null, { view: 'object' }, store)
  assert.equal(readAppNavLocation(1, null, store), null)
})

test('a tampered stored entry is never handed to a frame', () => {
  const store = memoryStore({
    'mobius:app-nav-location:1': JSON.stringify({ instance: null, location: '{oops' }),
    'mobius:app-nav-location:2': '{not json',
  })
  assert.equal(readAppNavLocation(1, null, store), null)
  assert.equal(readAppNavLocation(2, null, store), null)
})

test('the size bound counts UTF-8 bytes of the JSON text', () => {
  const fits = 'é'.repeat((APP_NAV_LOCATION_MAX_BYTES - 2) / 2)
  assert.equal(encodeNavLocation(fits), JSON.stringify(fits))
  assert.throws(() => encodeNavLocation(`${fits}é`), RangeError)
  assert.equal(validNavLocationText(JSON.stringify(`${fits}é`)), null)
  assert.equal(encodeNavLocation(undefined), null)
  assert.equal(validNavLocationText('null'), null)
})

test('logout clears every app location and nothing else', () => {
  const store = memoryStore({ unrelated: 'keep' })
  writeAppNavLocation(1, null, '{"view":"one"}', store)
  writeAppNavLocation(2, null, '{"view":"two"}', store)
  clearAppNavLocations(store)
  assert.deepEqual([...store.items.keys()], ['unrelated'])
})
