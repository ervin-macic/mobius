/* Bulk notification actions use neutral text rather than unread accent color. */
import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

test('mark all as read follows the theme text color', () => {
  const css = readFileSync(new URL('../NotificationsView.css', import.meta.url), 'utf8')
  const rule = css.match(/\.notifications__mark-all\s*\{([^}]+)\}/)[1]
  assert.match(rule, /color:\s*var\(--text\)/)
  assert.doesNotMatch(rule, /color:\s*var\(--accent\)/)
})
