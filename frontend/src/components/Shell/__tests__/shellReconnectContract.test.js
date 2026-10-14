// Exercise the owning live-list read against real query cancellation and a controlled API.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { QueryClient } from '@tanstack/react-query'
import { api } from '../../../api/client.js'
import { fetchFreshShellList } from '../shellListReconciliation.js'
import {
  applyAppActivityEvent,
  applyAppActivitySeenEvent,
  reconcileSeenAppActivity,
  rememberSeenAppActivity,
  withAppActivitySeen,
} from '../newAppAttention.js'
import { chatQueries, appQueries } from '../../../hooks/queries.js'

function freshReader(t, queryClient, fetch, kind = 'chats') {
  t.mock.method(api[kind], 'list', async options => ({
    ok: true,
    json: async () => fetch(options),
  }))
  return options => fetchFreshShellList(queryClient, kind === 'chats' ? chatQueries : appQueries, { timeoutMs: 5000, ...options })
}

test('fresh chat reconciliation requests live-only truth and replaces the old cached row', async t => {
  const client = new QueryClient()
  client.setQueryData(['chats'], [{ pending_question_id: 'answered' }])
  let options
  const fresh = freshReader(t, client, async value => { options = value; return [{ pending_question_id: null }] })
  try {
    await fresh()
    assert.equal(options.cache, 'no-store')
    assert.equal(options.timeoutMs, 5000)
    assert.equal(options.signal.aborted, false)
    assert.equal(client.getQueryData(['chats'])[0].pending_question_id, null)
  } finally { client.clear() }
})

test('an aborted reconnect cannot commit late list data over a newer projection', async t => {
  const client = new QueryClient()
  const controller = new AbortController()
  let release, receivedSignal
  const fresh = freshReader(t, client, options => {
    receivedSignal = options.signal
    return new Promise(resolve => { release = resolve })
  })
  try {
    const pending = fresh({ signal: controller.signal })
    const rejected = assert.rejects(pending, error => error.name === 'AbortError')
    while (!release) await Promise.resolve()
    controller.abort()
    client.setQueryData(['chats'], [{ pending_question_id: 'new-question' }])
    release([{ pending_question_id: 'obsolete-question' }])
    await rejected
    assert.equal(receivedSignal.aborted, true)
    assert.equal(client.getQueryData(['chats'])[0].pending_question_id, 'new-question')
  } finally { client.clear() }
})

test('abort during query cancellation cannot start an obsolete replacement read', async t => {
  const controller = new AbortController()
  let finishCancel, fetches = 0
  const fresh = freshReader(t, {
    cancelQueries: () => new Promise(resolve => { finishCancel = resolve }),
    fetchQuery: () => { fetches++; return Promise.resolve([]) },
  }, () => [])
  const pending = fresh({ signal: controller.signal })
  const rejected = assert.rejects(pending, error => error.name === 'AbortError')
  controller.abort(); finishCancel(); await rejected
  assert.equal(fetches, 0)
})

for (const kind of ['chats', 'apps']) {
  test(`${kind}: a fresh read replaces an in-flight pre-transition snapshot`, async t => {
    const client = new QueryClient()
    let releaseOld
    const old = client.fetchQuery({ queryKey: [kind], queryFn: () => new Promise(resolve => { releaseOld = resolve }) })
    const cancelled = assert.rejects(old)
    const fresh = freshReader(t, client, options => {
      assert.equal(options.cache, 'no-store')
      return [{ id: 'newly-created' }]
    }, kind)
    try {
      assert.deepEqual(await fresh(), [{ id: 'newly-created' }])
      releaseOld([{ id: 'obsolete' }]); await cancelled
      assert.deepEqual(client.getQueryData([kind]), [{ id: 'newly-created' }])
    } finally { client.clear() }
  })

  test(`${kind}: failure is not reported as successful fresh truth`, async t => {
    const client = new QueryClient()
    client.setQueryData([kind], [{ id: 'offline-copy' }])
    const fresh = freshReader(t, client, () => { throw new Error('unreachable') }, kind)
    try {
      await assert.rejects(fresh(), /unreachable/)
      assert.deepEqual(client.getQueryData([kind]), [{ id: 'offline-copy' }])
    } finally { client.clear() }
  })
}

test('a stale apps-list response resolving after another tab seen receipt cannot restore unread activity', async t => {
  const client = new QueryClient()
  const seen = new Map()
  const rows = [{ id: 7, created_at: 'current', has_unseen_activity: true, unseen_activity_version: 5 }]
  client.setQueryData(appQueries.keys.all, rows)
  let release
  const fresh = freshReader(t, client, () => new Promise(resolve => { release = resolve }), 'apps')
  try {
    const pending = fresh({ reconcile: data => reconcileSeenAppActivity(data, seen) })
    while (!release) await Promise.resolve()
    rememberSeenAppActivity(seen, 7, 'current', 5)
    client.setQueryData(appQueries.keys.all, data => withAppActivitySeen(data, 7, 5, 'current'))
    release(rows)
    await pending
    assert.equal(client.getQueryData(appQueries.keys.all)[0].has_unseen_activity, false)
    assert.equal(client.getQueryData(appQueries.keys.all)[0].unseen_activity_version, null)
  } finally { client.clear() }
})

const activityRow = (overrides = {}) => ({
  id: 7, created_at: 'current', has_unseen_activity: false, unseen_activity_version: null, ...overrides,
})
const activityEvent = (overrides = {}) => ({
  type: 'app_activity', appId: '7', unseenActivityVersion: 4, appCreatedAt: 'current', ...overrides,
})

test('a versioned app_activity event marks the one cached app without a refetch', () => {
  const client = new QueryClient()
  client.setQueryData(appQueries.keys.all, [activityRow(), activityRow({ id: 8, created_at: 'other' })])
  try {
    assert.equal(applyAppActivityEvent(client, appQueries.keys.all, activityEvent(), new Map()), true)
    const [seven, eight] = client.getQueryData(appQueries.keys.all)
    assert.equal(seven.has_unseen_activity, true)
    assert.equal(seven.unseen_activity_version, 4)
    assert.equal(eight.has_unseen_activity, false)
  } finally { client.clear() }
})

test('badge-only, uncached or other-lifetime app_activity events fall back to a full refetch', () => {
  const client = new QueryClient()
  const rows = [activityRow()]
  client.setQueryData(appQueries.keys.all, rows)
  try {
    for (const ev of [
      { type: 'app_activity', appId: '7' },
      activityEvent({ appId: '9' }),
      activityEvent({ appCreatedAt: 'recreated' }),
    ]) {
      assert.equal(applyAppActivityEvent(client, appQueries.keys.all, ev, new Map()), false)
    }
    assert.equal(client.getQueryData(appQueries.keys.all), rows)
  } finally { client.clear() }
})

test('an app_activity event arriving while the list is fetching defers to that refetch', async t => {
  const client = new QueryClient()
  const rows = [activityRow()]
  client.setQueryData(appQueries.keys.all, rows)
  let release
  const fresh = freshReader(t, client, () => new Promise(resolve => { release = resolve }), 'apps')
  try {
    const pending = fresh()
    while (!release) await Promise.resolve()
    assert.equal(applyAppActivityEvent(client, appQueries.keys.all, activityEvent(), new Map()), false)
    assert.equal(client.getQueryData(appQueries.keys.all), rows)
    release(rows)
    await pending
  } finally { client.clear() }
})

test('app_activity_seen clears the cached dot and stops a delayed older event re-lighting it', () => {
  const client = new QueryClient()
  const seen = new Map()
  client.setQueryData(appQueries.keys.all, [activityRow({ has_unseen_activity: true, unseen_activity_version: 4 })])
  try {
    applyAppActivitySeenEvent(client, appQueries.keys.all, {
      type: 'app_activity_seen', appId: '7', appCreatedAt: 'current', seenThroughVersion: 4,
    }, seen)
    assert.equal(client.getQueryData(appQueries.keys.all)[0].has_unseen_activity, false)
    assert.equal(applyAppActivityEvent(client, appQueries.keys.all, activityEvent({ unseenActivityVersion: 3 }), seen), true)
    assert.equal(client.getQueryData(appQueries.keys.all)[0].has_unseen_activity, false)
    assert.equal(applyAppActivityEvent(client, appQueries.keys.all, activityEvent({ unseenActivityVersion: 5 }), seen), true)
    assert.equal(client.getQueryData(appQueries.keys.all)[0].unseen_activity_version, 5)
  } finally { client.clear() }
})
