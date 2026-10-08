import test from 'node:test'
import assert from 'node:assert/strict'
import { QueryClient, QueryObserver } from '@tanstack/query-core'

import { invalidateEventFedCachesOnOpen } from '../eventFedCaches.js'

const KEY = ['auth', 'providers', 'status']

function clientWith(updatedAt) {
  const client = new QueryClient()
  client.setQueryData(KEY, { mobius: { authenticated: true } }, { updatedAt })
  return client
}

test('first open refreshes provider status restored from a previous page load', async () => {
  const pageLoadedAt = 1_000_000
  const client = clientWith(pageLoadedAt - 60_000)
  await Promise.all(invalidateEventFedCachesOnOpen(client))
  assert.equal(client.getQueryState(KEY).isInvalidated, true,
    'a persisted entry inside its staleTime would otherwise make no request at all')
})

test('first open invalidates provider status fetched before subscription on this page', async () => {
  const pageLoadedAt = 1_000_000
  const client = clientWith(pageLoadedAt + 50)
  await Promise.all(invalidateEventFedCachesOnOpen(client))
  assert.equal(client.getQueryState(KEY).isInvalidated, true)
})

test('remount invalidates non-persisted caches fetched by the previous Shell', async () => {
  const pageLoadedAt = 1_000_000
  const client = clientWith(pageLoadedAt - 60_000)
  client.setQueryData(['models', 'registry'], [], { updatedAt: pageLoadedAt + 50 })
  await Promise.all(invalidateEventFedCachesOnOpen(client))
  assert.equal(client.getQueryState(['models', 'registry']).isInvalidated, true)
})

test('first open invalidates a model registry snapshot taken before subscription', async () => {
  const client = new QueryClient()
  client.setQueryData(['models', 'registry'], ['old-model'], { updatedAt: 1050 })
  await Promise.all(invalidateEventFedCachesOnOpen(client))
  assert.equal(client.getQueryState(['models', 'registry']).isInvalidated, true)
})

test('every open invalidates all seven event-fed domains but leaves unrelated reads alone', async () => {
  const client = new QueryClient()
  const keys = [
    ['models', 'registry'], ['auth', 'providers', 'status'],
    ['app-source', 'app-1'], ['chat-app-artifacts', 'chat-1'],
    ['chat-activity', 'chat-1'], ['projects', 'files', 'project-1'],
    ['projects', 'git', 'project-1'],
  ]
  for (const key of [...keys, ['unrelated']]) client.setQueryData(key, ['snapshot'])
  await Promise.all(invalidateEventFedCachesOnOpen(client))
  for (const key of keys) assert.equal(client.getQueryState(key).isInvalidated, true)
  assert.equal(client.getQueryState(['unrelated']).isInvalidated, false)
})

test('a missed pre-subscription model event is repaired by the opening read', async () => {
  const client = new QueryClient()
  const key = ['models', 'registry']
  let serverModels = ['old-model']
  let reads = 0
  const observer = new QueryObserver(client, {
    queryKey: key,
    staleTime: 300_000,
    queryFn: async () => { reads++; return serverModels },
  })
  const unsubscribe = observer.subscribe(() => {})
  try {
    await observer.refetch()
    assert.deepEqual(client.getQueryData(key), ['old-model'])
    // The server publishes the change while no subscription exists yet.
    serverModels = ['new-model']
    const beforeOpenReads = reads
    await Promise.all(invalidateEventFedCachesOnOpen(client))
    assert.ok(reads > beforeOpenReads, 'the first stream open must cross the subscription barrier')
    assert.deepEqual(client.getQueryData(key), ['new-model'])
  } finally {
    unsubscribe()
    client.clear()
  }
})
