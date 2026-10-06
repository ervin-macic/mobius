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
  writeValidatedAppNavLocation,
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
  writeValidatedAppNavLocation(1, 'nonce-a', '{"view":"one"}', store)
  writeValidatedAppNavLocation(2, 'nonce-b', '{"view":"two"}', store)

  assert.equal(readAppNavLocation(1, 'nonce-a', store), '{"view":"one"}')
  assert.equal(readAppNavLocation(2, 'nonce-b', store), '{"view":"two"}')
  assert.equal(readAppNavLocation(3, 'nonce-a', store), null)
  assert.equal(readAppNavLocation(1, 'nonce-b', store), null, 'a reused id or wiped data starts fresh')
})

test('a cleared report removes the entry', () => {
  const store = memoryStore()
  writeValidatedAppNavLocation(1, null, '{"view":"one"}', store)
  writeValidatedAppNavLocation(1, null, null, store)
  assert.equal(store.items.size, 0)
})

test('the shared wire validator rejects invalid reports before persistence', () => {
  const invalid = ['alert(1)', '{broken', { view: 'object' },
    JSON.stringify({ q: 'x'.repeat(APP_NAV_LOCATION_MAX_BYTES) })]
  for (const value of invalid) {
    assert.equal(validNavLocationText(value), null)
  }
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
  writeValidatedAppNavLocation(1, null, '{"view":"one"}', store)
  writeValidatedAppNavLocation(2, null, '{"view":"two"}', store)
  clearAppNavLocations(store)
  assert.deepEqual([...store.items.keys()], ['unrelated'])
})
