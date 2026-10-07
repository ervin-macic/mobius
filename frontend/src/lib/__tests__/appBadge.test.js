import test from 'node:test'
import assert from 'node:assert/strict'
import { appBadgeLabel } from '../../components/Drawer/appBadge.js'

test('no pill for an empty, cleared, or malformed count', () => {
  for (const count of [0, -3, undefined, null, '', 'abc', Number.NaN, 0.5]) {
    assert.equal(appBadgeLabel(count), null, `count ${String(count)}`)
  }
})

test('counts show as whole numbers up to the cap', () => {
  assert.equal(appBadgeLabel(1), '1')
  assert.equal(appBadgeLabel('7'), '7')
  assert.equal(appBadgeLabel(99), '99')
})

test('counts past the cap show as 99+', () => {
  assert.equal(appBadgeLabel(100), '99+')
  assert.equal(appBadgeLabel(1_000_000), '99+')
  assert.equal(appBadgeLabel(Number.POSITIVE_INFINITY), null)
})
