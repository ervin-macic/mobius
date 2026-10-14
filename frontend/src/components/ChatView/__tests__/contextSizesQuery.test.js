/* /context measures again each time it opens, so no compaction leaves it stale. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { api } from '../../../api/client.js'
import { chatQueries } from '../../../hooks/queries.js'

const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve() }

test('opening /context re-reads sizes even when a fresh answer is cached', async (t) => {
  const original = api.chats.contextSizes
  t.after(() => { api.chats.contextSizes = original })
  const reads = []
  api.chats.contextSizes = async (chatId) => {
    reads.push(chatId)
    return Response.json({ parts: { summary: 10 } })
  }
  const client = new QueryClient()
  t.after(() => client.clear())
  // Measured a moment ago, before a compaction this composer never saw
  // (an automatic run, another device): still "fresh" by age alone.
  client.setQueryData(chatQueries.keys.contextSizes('c1'), { parts: { summary: 4000 } })

  const observer = new QueryObserver(client, chatQueries.contextSizes.options('c1'))
  const unsubscribe = observer.subscribe(() => {})
  t.after(unsubscribe)
  // The last answer shows while the new one loads.
  assert.deepEqual(observer.getCurrentResult().data, { parts: { summary: 4000 } })
  await flush()

  assert.deepEqual(reads, ['c1'])
  assert.deepEqual(client.getQueryData(chatQueries.keys.contextSizes('c1')), { parts: { summary: 10 } })
})
