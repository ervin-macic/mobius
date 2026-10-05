/* Guest bearer renewal never reaches the owner's token or persisted cache. */
import test from 'node:test'
import assert from 'node:assert/strict'

// Cookie ownership needs no cross-tab coordination, so every test runs in a
// browser without Web Locks.
if (globalThis.navigator) Object.defineProperty(globalThis.navigator, 'locks', { configurable: true, value: undefined })

test('redeem, one 401 renewal, and failed renewal stay memory-only', async () => {
  const owner = new Map([['token', 'owner-bearer']])
  const ownerAccesses = []
  globalThis.localStorage = {
    getItem: key => { ownerAccesses.push(['read', key]); return owner.get(key) ?? null },
    setItem: (key, value) => { ownerAccesses.push(['write', key]); owner.set(key, value) },
    removeItem: key => { ownerAccesses.push(['delete', key]); owner.delete(key) },
  }
  globalThis.window = { dispatchEvent() {} }
  globalThis.location = { pathname: '/shell/shared' }
  const calls = []
  let protectedCalls = 0
  let failRenewal = false
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    if (url.endsWith('/browser-access/session/redeem')) {
      assert.equal(options.credentials, 'same-origin')
      assert.deepEqual(JSON.parse(options.body), { invite: 'one-use-secret' })
      assert.equal(options.headers.Authorization, undefined)
      return new Response(JSON.stringify({ access_token: 'guest-1', token_type: 'bearer', grant: { id: 'g1', label: 'Friend' }, expires_in: 900 }), { status: 200 })
    }
    if (url.endsWith('/browser-access/session')) {
      assert.equal(options.headers.Authorization, undefined)
      if (failRenewal) return new Response('', { status: 401 })
      return new Response(JSON.stringify({ access_token: 'guest-2', token_type: 'bearer', grant: { id: 'g1', label: 'Friend' }, expires_in: 900 }), { status: 200 })
    }
    if (url.endsWith('/api/chats?shared_browser=1')) {
      protectedCalls += 1
      assert.equal(options.headers.Authorization, `Bearer guest-${protectedCalls}`)
      return new Response('', { status: protectedCalls === 1 ? 401 : 200 })
    }
    assert.fail(`unexpected URL ${url}`)
  }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  await client.redeemSharedBrowserInvite('one-use-secret')
  assert.equal(client.getToken(), 'guest-1')
  assert.equal((await client.apiFetch('/chats')).status, 200)
  assert.equal(client.getToken(), 'guest-2')
  failRenewal = true
  await assert.rejects(client.renewSharedBrowserSession(), /SHARED_ACCESS_ENDED/)
  assert.equal(client.getToken(), null)
  assert.equal(owner.get('token'), 'owner-bearer')
  assert.deepEqual(ownerAccesses, [])
  assert.equal(calls.filter(call => call.url.endsWith('/browser-access/session')).length, 2)
})

test('renewal cannot silently switch an active tab to another grant', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const owner = new Map([['token', 'owner-bearer']])
  globalThis.localStorage = {
    getItem: key => owner.get(key) ?? null,
    setItem: () => assert.fail('owner write'),
    removeItem: () => assert.fail('owner delete'),
  }
  globalThis.fetch = async url => {
    const grant = url.endsWith('/redeem') ? 'grant-A' : 'grant-B'
    return new Response(JSON.stringify({
      access_token: `token-${grant}`, token_type: 'bearer',
      grant: { id: grant, label: grant }, expires_in: 900,
    }), { status: 200 })
  }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  await client.redeemSharedBrowserInvite('grant-A-invite')
  await assert.rejects(client.renewSharedBrowserSession(), /SHARED_ACCESS_GRANT_CHANGED/)
  assert.equal(client.getToken(), null)
  assert.equal(owner.get('token'), 'owner-bearer')
})

test('leave targets the current grant and reports a cookie mismatch for retry', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  let logoutAttempts = 0
  globalThis.fetch = async (url, options) => {
    if (url.endsWith('/redeem')) return new Response(JSON.stringify({
      access_token: 'grant-A-token', token_type: 'bearer',
      grant: { id: 'grant-A', label: 'A' }, expires_in: 900,
    }), { status: 200 })
    assert.ok(url.endsWith('/session/logout'))
    assert.deepEqual(JSON.parse(options.body), { grant_id: 'grant-A' })
    assert.equal(options.credentials, 'same-origin')
    assert.equal(options.headers.Authorization, undefined)
    logoutAttempts += 1
    return new Response(null, { status: logoutAttempts === 1 ? 409 : 204 })
  }
  const client = await import('../../api/client.js')
  await client.redeemSharedBrowserInvite('another-invite')
  await assert.rejects(client.leaveSharedBrowserSession(), /SHARED_ACCESS_LOGOUT_FAILED/)
  assert.equal(client.getToken(), null)
  assert.equal(await client.leaveSharedBrowserSession(), true)
  assert.equal(logoutAttempts, 2)
})

test('theme reads use one-use cache keys so an older SW cannot replay a revoked session', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const themeUrls = []
  globalThis.fetch = async url => {
    if (url.endsWith('/session/redeem')) return new Response(JSON.stringify({
      access_token: 'guest-token', token_type: 'bearer',
      grant: { id: 'grant-theme', label: 'Theme' }, expires_in: 900,
    }), { status: 200 })
    if (url.includes('/api/theme?shared_browser=')) {
      themeUrls.push(url)
      return new Response('{}', { status: 200 })
    }
    assert.fail(`unexpected URL ${url}`)
  }
  const client = await import('../../api/client.js')
  await client.redeemSharedBrowserInvite('theme-invite')
  await client.apiFetch('/theme')
  await client.apiFetch('/theme')
  assert.equal(themeUrls.length, 2)
  assert.notEqual(themeUrls[0], themeUrls[1])
})

test('A mutation and stale success never cross into B authority', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  let releaseA
  let aStarted
  const started = new Promise(resolve => { aStarted = resolve })
  const waiting = new Promise(resolve => { releaseA = resolve })
  let redeems = 0
  const mutations = []
  globalThis.fetch = async (url, options) => {
    if (url.endsWith('/redeem')) {
      redeems += 1
      return new Response(JSON.stringify({ access_token: redeems === 1 ? 'A' : 'B', token_type: 'bearer', grant: { id: redeems === 1 ? 'A' : 'B' }, expires_in: 900 }), { status: 200 })
    }
    if (url.endsWith('/api/chats')) {
      mutations.push(options.headers.Authorization)
      aStarted()
      await waiting
      return new Response('', { status: 401 })
    }
    assert.fail(`unexpected ${url}`)
  }
  await client.redeemSharedBrowserInvite('A')
  const oldMutation = client.apiFetch('/chats', { method: 'POST', body: '{}' })
  await started
  await client.redeemSharedBrowserInvite('B')
  releaseA()
  await assert.rejects(oldMutation, /SHARED_ACCESS_SUPERSEDED/)
  assert.deepEqual(mutations, ['Bearer A'])
  assert.equal(client.getToken(), 'B')
})

test('successful old-grant response is rejected after B redeem', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  let releaseOld
  let oldStarted
  const started = new Promise(resolve => { oldStarted = resolve })
  const waiting = new Promise(resolve => { releaseOld = resolve })
  let redeems = 0
  globalThis.fetch = async url => {
    if (url.endsWith('/redeem')) {
      redeems += 1
      const grant = redeems === 1 ? 'A' : 'B'
      return new Response(JSON.stringify({ access_token: grant, token_type: 'bearer', grant: { id: grant }, expires_in: 900 }), { status: 200 })
    }
    oldStarted()
    await waiting
    return new Response('{}', { status: 200 })
  }
  await client.redeemSharedBrowserInvite('A')
  const oldRead = client.apiFetch('/old')
  await started
  await client.redeemSharedBrowserInvite('B')
  releaseOld()
  await assert.rejects(oldRead, /SHARED_ACCESS_SUPERSEDED/)
  assert.equal(client.getToken(), 'B')
})

test('hash clear invalidates a pending redeem without installing or logging it out', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  let releaseA
  let aStarted
  const started = new Promise(resolve => { aStarted = resolve })
  const waiting = new Promise(resolve => { releaseA = resolve })
  const calls = []
  globalThis.fetch = async url => {
    if (url.endsWith('/redeem')) {
      calls.push('redeem:A')
      aStarted()
      await waiting
      return new Response(JSON.stringify({ access_token: 'A', token_type: 'bearer', grant: { id: 'A' }, expires_in: 900 }), { status: 200 })
    }
    assert.fail(`unexpected ${url}`)
  }
  const old = client.redeemSharedBrowserInvite('A')
  const rejected = assert.rejects(old, /SHARED_ACCESS_SUPERSEDED/)
  await started
  client.clearSharedBrowserSession() // New #invite arrived; B not accepted yet.
  releaseA()
  await rejected
  assert.deepEqual(calls, ['redeem:A'])
  assert.equal(client.getToken(), null)
})

test('a renewal that loses to a newer sign-in neither installs nor ends it', async () => {
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  let renewalStarted
  let finishRenewal
  const started = new Promise(resolve => { renewalStarted = resolve })
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push(url.replace(/.*browser-access\//, ''))
    if (url.endsWith('/session/redeem')) {
      const grant = JSON.parse(options.body).invite
      return new Response(JSON.stringify({ access_token: grant, token_type: 'bearer', grant: { id: grant }, expires_in: 900 }), { status: 200 })
    }
    assert.ok(url.endsWith('/session'))
    renewalStarted()
    return new Promise(resolve => { finishRenewal = resolve })
  }
  await client.redeemSharedBrowserInvite('A')
  for (const status of [401, 200]) {
    const renewal = client.renewSharedBrowserSession()
    assert.equal(client.renewSharedBrowserSession(), renewal)
    await started
    await client.redeemSharedBrowserInvite('B')
    finishRenewal(new Response(JSON.stringify({ access_token: 'stale', token_type: 'bearer', grant: { id: 'A' }, expires_in: 900 }), { status }))
    await assert.rejects(renewal, status === 401 ? /SHARED_ACCESS_ENDED/ : /SHARED_ACCESS_SUPERSEDED/)
    assert.equal(client.getToken(), 'B')
  }
  assert.deepEqual(calls, ['session/redeem', 'session', 'session/redeem', 'session', 'session/redeem'])
})

test('account finalization is spent once and never installs after its tab moved on', async () => {
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  const calls = []
  let finishAccount
  let started
  const ready = new Promise(resolve => { started = resolve })
  globalThis.fetch = async (url, options) => {
    calls.push(url)
    assert.ok(url.endsWith('/session/account/finalize'))
    assert.equal(options.credentials, 'same-origin')
    assert.deepEqual(JSON.parse(options.body), { pending_id: 'specific-account-flow' })
    assert.equal(options.headers.Authorization, undefined)
    started()
    return new Promise(resolve => { finishAccount = resolve })
  }
  const pending = client.finalizeSharedBrowserAccount('specific-account-flow')
  assert.equal(client.finalizeSharedBrowserAccount('specific-account-flow'), pending)
  await ready
  client.clearSharedBrowserSession()
  finishAccount(new Response(JSON.stringify({ access_token: 'must-not-install', token_type: 'bearer', grant: { id: 'account-A' }, expires_in: 900 }), { status: 200 }))
  await assert.rejects(pending, /SHARED_ACCESS_SUPERSEDED/)
  assert.equal(calls.length, 1)
  assert.equal(client.getToken(), null)
})

test('account finalization installs its grant in a browser without Web Locks', async () => {
  assert.equal(globalThis.navigator?.locks, undefined)
  globalThis.location = { pathname: '/shell/shared' }
  globalThis.window = { dispatchEvent() {} }
  const client = await import('../../api/client.js')
  client.beginSharedBrowserAuth()
  globalThis.fetch = async url => {
    if (url.endsWith('/session/account/finalize')) return new Response(JSON.stringify({ access_token: 'account-1', token_type: 'bearer', grant: { id: 'account' }, expires_in: 900 }), { status: 200 })
    if (url.endsWith('/session')) return new Response(JSON.stringify({ access_token: 'account-2', token_type: 'bearer', grant: { id: 'account' }, expires_in: 900 }), { status: 200 })
    assert.equal(url, '/api/connect/browser-access/session/logout')
    return new Response(null, { status: 204 })
  }
  await client.finalizeSharedBrowserAccount('flow-without-locks')
  assert.equal(client.getToken(), 'account-1')
  await client.renewSharedBrowserSession()
  assert.equal(client.getToken(), 'account-2')
  assert.equal(await client.leaveSharedBrowserSession(), true)
  assert.equal(client.getToken(), null)
})
