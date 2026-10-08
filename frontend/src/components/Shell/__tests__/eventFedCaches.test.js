import test from 'node:test'
import assert from 'node:assert/strict'
import { QueryClient } from '@tanstack/query-core'

import { invalidateRestoredEventFedCaches } from '../eventFedCaches.js'

const KEY = ['auth', 'providers', 'status']

function clientWith(updatedAt) {
  const client = new QueryClient()
  client.setQueryData(KEY, { mobius: { authenticated: true } }, { updatedAt })
  return client
}

test('first open refreshes provider status restored from a previous page load', async () => {
  const pageLoadedAt = 1_000_000
  const client = clientWith(pageLoadedAt - 60_000)
  await Promise.all(invalidateRestoredEventFedCaches(client, pageLoadedAt))
  assert.equal(client.getQueryState(KEY).isInvalidated, true,
    'a persisted entry inside its staleTime would otherwise make no request at all')
})

test('first open leaves provider status this page already fetched alone', async () => {
  const pageLoadedAt = 1_000_000
  const client = clientWith(pageLoadedAt + 50)
  await Promise.all(invalidateRestoredEventFedCaches(client, pageLoadedAt))
  assert.equal(client.getQueryState(KEY).isInvalidated, false)
})

test('first open does not touch other event-fed caches', async () => {
  const pageLoadedAt = 1_000_000
  const client = clientWith(pageLoadedAt - 60_000)
  client.setQueryData(['models', 'registry'], [], { updatedAt: pageLoadedAt - 60_000 })
  await Promise.all(invalidateRestoredEventFedCaches(client, pageLoadedAt))
  assert.equal(client.getQueryState(['models', 'registry']).isInvalidated, false)
})
