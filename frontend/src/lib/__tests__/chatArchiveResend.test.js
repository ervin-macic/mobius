import assert from 'node:assert/strict'
import test from 'node:test'

import { api } from '../../api/client.js'

function stubFetch(t, outcomes) {
  const originalFetch = globalThis.fetch
  const calls = []
  globalThis.fetch = async (url, options) => {
    // A dropped connection also triggers a reachability probe; answer it
    // without consuming the archive call's scripted outcomes.
    if (!/\/chats\/[^/]+\/(un)?archive$/.test(String(url))) {
      return { ok: true, status: 200, json: async () => ({}) }
    }
    calls.push({ url: String(url), method: options?.method })
    const next = outcomes.shift()
    if (next instanceof Error) throw next
    return { ok: next < 400, status: next, json: async () => ({}) }
  }
  t.after(() => { globalThis.fetch = originalFetch })
  return calls
}

test('archive resends once when the connection drops before any response', async (t) => {
  const calls = stubFetch(t, [new TypeError('Load failed'), 200])
  const response = await api.chats.archive('chat-1')
  assert.equal(response.ok, true)
  assert.equal(calls.length, 2)
  assert.ok(calls.every(call => call.method === 'POST' && call.url.endsWith('/chats/chat-1/archive')))
})

test('restore resends once on a dropped connection, then reports the second failure', async (t) => {
  const calls = stubFetch(t, [new TypeError('Failed to fetch'), new TypeError('Failed to fetch')])
  await assert.rejects(api.chats.unarchive('chat-1'), TypeError)
  assert.equal(calls.length, 2, 'never more than one resend')
})

test('a server answer is final: an error status is not resent', async (t) => {
  const calls = stubFetch(t, [500])
  const response = await api.chats.archive('chat-1')
  assert.equal(response.ok, false)
  assert.equal(calls.length, 1)
})

test('a deliberate abort is not resent', async (t) => {
  const abort = new DOMException('aborted', 'AbortError')
  const calls = stubFetch(t, [abort])
  await assert.rejects(api.chats.archive('chat-1'), error => error.name === 'AbortError')
  assert.equal(calls.length, 1)
})
