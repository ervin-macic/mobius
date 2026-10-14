/* Compaction readback never owns action latches or crosses a visible chat lifecycle. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { api } from '../../../../api/client.js'
import { persistComposerDraft, readComposerDraft } from '../../composerDraft.js'
import useCompactCommands from '../useCompactCommands.js'
import useComposerDraftState from '../useComposerDraftState.js'
import { setActiveSharedBrowserGrantId } from '../../../../lib/sharedBrowserWorkspace.js'
import { renderHook } from './react-hook-shim.mjs'

const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve() }
const response = body => Response.json(body)
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function installApi(t) {
  const reads = [], posts = [], stops = []
  for (const name of ['compact', 'compactStop', 'compactProgress']) {
    const original = api.chats[name]
    t.after(() => { api.chats[name] = original })
  }
  api.chats.compactProgress = (chatId, options) => {
    const pending = deferred()
    reads.push({ chatId, options, ...pending })
    return pending.promise
  }
  api.chats.compact = (chatId, body) => {
    const pending = deferred()
    posts.push({ chatId, body, ...pending })
    return pending.promise
  }
  api.chats.compactStop = chatId => {
    const pending = deferred()
    stops.push({ chatId, ...pending })
    return pending.promise
  }
  return { reads, posts, stops }
}

function setup(t, overrides = {}) {
  const { reads, posts, stops } = installApi(t)
  const drafts = [], failures = [], messages = [], turnStops = []
  const props = {
    chatId: 'a', provisionalNewChat: false, hidden: false, serverCompactingKind: null,
    activationSettledRef: { current: true }, inputValueRef: { current: '' },
    setComposerInput: text => drafts.push(text), setSendFailure: error => failures.push(error),
    fetchMessages: async options => messages.push(options),
    stopTurn: () => turnStops.push('turn'),
    ...overrides,
  }
  const hook = renderHook(useCompactCommands, props)
  t.after(() => hook.unmount())
  return { hook, props, reads, posts, stops, drafts, failures, messages, turnStops }
}

test('resolved compact POST releases busy and admission while optional status GET stays deferred', async t => {
  const { hook, reads, posts, drafts, messages } = setup(t)
  const first = hook.result.current.runCompactCommand('Keep decisions', '/compact Keep decisions')
  assert.equal(hook.result.current.preparing, true)
  await hook.result.current.runCompactCommand()
  assert.equal(posts.length, 1, 'duplicate action during POST is blocked')
  posts[0].resolve(response({ ok: true, progress: { state: 'completed' } }))
  await first
  assert.equal(hook.result.current.preparing, false)
  assert.equal(reads.length, 2)
  assert.equal(reads[0].options.signal.aborted, true, 'superseded entry GET canceled')
  assert.equal(reads[1].options.signal.aborted, false)
  assert.deepEqual(messages, [{ force: true }])
  assert.deepEqual(drafts, [''])
  const second = hook.result.current.runCompactCommand('', null, 'recovery-a')
  assert.equal(posts.length, 2, 'deferred readback cannot block another explicit action')
  assert.equal(posts[1].body.recovery_id, 'recovery-a')
  assert.deepEqual(drafts, [''], 'card action preserves draft')
  reads[1].reject(new Error('status failed'))
  await flush()
  assert.equal(hook.result.current.compactionProgress.state, 'completed')
  posts[1].resolve(response({ progress: { state: 'paused', recovery_id: 'recovery-a' } }))
  await second
  assert.equal(posts.length, 2, 'readback never auto-continues')
})

test('a completed compaction reports its chat so cached context sizes drop', async t => {
  const compacted = []
  const { hook, posts } = setup(t, { onCompacted: chatId => compacted.push(chatId) })
  const action = hook.result.current.runCompactCommand()
  posts[0].resolve(response({ ok: true, progress: { state: 'completed' } }))
  await action
  assert.deepEqual(compacted, ['a'], 'the /context parts cache is invalidated once')
  // A failed compaction changed nothing, so the cached breakdown stands.
  const failed = hook.result.current.runCompactCommand()
  posts[1].reject(new Error('compaction unavailable'))
  await failed
  assert.deepEqual(compacted, ['a'])
})

test('Pause POST releases its admission latch independently of deferred and failed status GETs', async t => {
  const { hook, reads, stops, failures } = setup(t)
  const first = hook.result.current.stopCompactCommand()
  await hook.result.current.stopCompactCommand()
  assert.equal(stops.length, 1)
  stops[0].resolve(response({ ok: true }))
  await first
  const second = hook.result.current.stopCompactCommand()
  assert.equal(stops.length, 2)
  reads[1].reject(new Error('readback unavailable'))
  await flush()
  assert.deepEqual(failures, [], 'optional GET failure is not an action failure')
  stops[1].resolve(response({ ok: true }))
  await second
  const third = hook.result.current.stopCompactCommand()
  assert.equal(stops.length, 3)
  stops[2].resolve(response({ ok: true }))
  await third
})

test('failed compact leaves the draft to its owner, reports the failure, and keeps the recovery handle', async t => {
  const { hook, props, reads, posts, drafts, failures } = setup(t)
  reads[0].resolve(response({ progress: { state: 'paused', recovery_id: 'saved' } }))
  await flush()
  const action = hook.result.current.runCompactCommand('', '/compact')
  props.inputValueRef.current = 'new owner draft'
  posts[0].reject(new Error('POST failed'))
  await action
  assert.equal(hook.result.current.preparing, false)
  assert.deepEqual(drafts, [], 'neither the command nor the newer draft is rewritten')
  assert.match(failures.at(-1), /couldn’t send/)
  reads[1].reject(new Error('GET failed'))
  await flush()
  assert.equal(hook.result.current.compactionProgress.recovery_id, 'saved')
})

test('a submitted command is never cleared before the server acknowledges it', async t => {
  const submitted = '/compact Preserve key decisions'
  const { hook, posts, drafts } = setup(t, { inputValueRef: { current: submitted } })
  const action = hook.result.current.runCompactCommand('Preserve key decisions', submitted)
  assert.deepEqual(drafts, [], 'the composer keeps the command while the POST is pending')
  posts[0].reject(new Error('POST failed'))
  await action
  assert.deepEqual(drafts, [], 'a failure has nothing to put back')
  assert.equal(hook.result.current.preparing, false)
})

test('chat switch cancels status GET and fences late GET/POST errors, draft restoration, and readback', async t => {
  const { hook, props, reads, posts, drafts, failures, messages } = setup(t)
  const action = hook.result.current.runCompactCommand()
  hook.rerender({ ...props, chatId: 'b' })
  assert.equal(reads[0].options.signal.aborted, true)
  reads[0].resolve(response({ progress: { state: 'paused', recovery_id: 'old' } }))
  reads[1].resolve(response({ progress: { state: 'paused', recovery_id: 'new' } }))
  posts[0].reject(new Error('old POST failed'))
  await action
  await flush()
  assert.equal(hook.result.current.compactionProgress.recovery_id, 'new')
  assert.deepEqual(drafts, [])
  assert.deepEqual(failures, [null])
  assert.deepEqual(messages, [])
  assert.equal(reads.length, 2, 'old action finally cannot read either chat again')
})

test('hide/unmount cancels reads and late POST completion cannot resurrect optional GETs', async t => {
  const { hook, props, reads, posts, stops } = setup(t)
  const action = hook.result.current.runCompactCommand('', null)
  hook.rerender({ ...props, hidden: true })
  assert.equal(reads[0].options.signal.aborted, true)
  posts[0].resolve(response({ progress: { state: 'paused' } }))
  await action
  assert.equal(reads.length, 1)
  hook.rerender(props)
  assert.equal(reads.length, 2)
  const stop = hook.result.current.stopCompactCommand()
  hook.unmount()
  assert.equal(reads[1].options.signal.aborted, true)
  stops[0].resolve(response({ ok: true }))
  await stop
  reads[1].resolve(response({ progress: { state: 'stale' } }))
  await flush()
  assert.equal(reads.length, 2, 'unmounted action cannot resurrect a GET')
})


test('progress GET deadline aborts the actual transport without erasing recovery or restarting work', async t => {
  const { hook, props, reads, posts } = setup(t)
  reads[0].resolve(response({ progress: { state: 'paused', recovery_id: 'saved' } }))
  await flush()
  // Restore the real API read to prove timeout and lifecycle signals compose.
  // setup registered restoration before this fixture replacement.
  const { apiFetch } = await import('../../../../api/client.js')
  api.chats.compactProgress = (chatId, options) => apiFetch(`/chats/${chatId}/compact-progress`, options)
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })
  const transports = []
  globalThis.fetch = (url, options) => {
    if (!url.endsWith('/compact-progress')) return Promise.resolve(response({ ok: true }))
    const pending = deferred()
    transports.push({ url, options, ...pending })
    options.signal.addEventListener('abort', () => pending.reject(options.signal.reason), { once: true })
    return pending.promise
  }
  t.mock.timers.enable({ apis: ['setTimeout'] })
  hook.rerender({ ...props, serverCompactingKind: 'compact' })
  assert.equal(transports.length, 1)
  t.mock.timers.tick(14999)
  assert.equal(transports[0].options.signal.aborted, false)
  t.mock.timers.tick(1)
  await flush()
  assert.equal(transports[0].options.signal.aborted, true)
  assert.equal(transports[0].options.signal.reason.name, 'TimeoutError')
  assert.equal(hook.result.current.compactionProgress.recovery_id, 'saved')
  t.mock.timers.tick(60000)
  await flush()
  assert.equal(transports.length, 1, 'there is no progress polling or timeout retry')
  assert.equal(posts.length, 0, 'timeout cannot automatically continue compaction')
})


test('a server compaction edge GET cannot supersede the authoritative paused POST result', async t => {
  const { hook, props, reads, posts } = setup(t)
  reads[0].resolve(response({ progress: { state: 'running' } }))
  await flush()
  const action = hook.result.current.runCompactCommand('', null)
  hook.rerender({ ...props, serverCompactingKind: 'compact' })
  assert.equal(reads.length, 2)
  posts[0].resolve(response({ progress: { state: 'paused', recovery_id: 'continue-me' } }))
  await action
  assert.equal(hook.result.current.preparing, true, 'the server window still owns preparation')
  assert.equal(hook.result.current.compactionProgress.state, 'paused')
  assert.equal(reads[1].options.signal.aborted, true)
  assert.equal(reads.length, 3, 'final readback stays deferred')
  reads[1].resolve(response({ progress: { state: 'running' } }))
  await flush()
  assert.equal(hook.result.current.compactionProgress.recovery_id, 'continue-me')
  assert.equal(posts.length, 1)
})

test('a POST from a departed chat activation cannot overwrite a later activation of the same chat', async t => {
  const { hook, props, reads, posts, drafts, failures, messages } = setup(t)
  const action = hook.result.current.runCompactCommand()
  hook.rerender({ ...props, chatId: 'b' })
  hook.rerender(props)
  reads[2].resolve(response({ progress: { state: 'paused', recovery_id: 'current' } }))
  await flush()
  posts[0].resolve(response({ ok: true, progress: { state: 'completed' } }))
  await action
  assert.equal(hook.result.current.compactionProgress.recovery_id, 'current')
  assert.equal(reads.length, 3)
  assert.deepEqual(drafts, [], 'a departed activation calls no pane setter')
  assert.deepEqual(failures, [null])
  assert.deepEqual(messages, [])
})

test('the progress deadline also cancels a shared-browser read without a transport timeout', async t => {
  const { hook, props, reads, posts } = setup(t)
  reads[0].resolve(response({ progress: { state: 'paused', recovery_id: 'saved' } }))
  await flush()
  const client = await import('../../../../api/client.js?compact-shared-deadline')
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })
  const transports = []
  globalThis.fetch = (url, options) => {
    if (url.endsWith('/browser-access/session/redeem')) return Promise.resolve(response({
      access_token: 'fixture-guest', token_type: 'bearer', expires_in: 900,
      grant: { id: 'fixture-grant' },
    }))
    assert.ok(url.endsWith('/compact-progress'))
    const pending = deferred()
    transports.push({ options, ...pending })
    options.signal.addEventListener('abort', () => pending.reject(options.signal.reason), { once: true })
    return pending.promise
  }
  client.beginSharedBrowserAuth()
  await client.redeemSharedBrowserInvite('fixture-invite')
  api.chats.compactProgress = (chatId, options) => client.apiFetch(`/chats/${chatId}/compact-progress`, options)
  t.mock.timers.enable({ apis: ['setTimeout'] })
  hook.rerender({ ...props, serverCompactingKind: 'compact' })
  await flush()
  assert.equal(transports.length, 1)
  t.mock.timers.tick(15000)
  await flush()
  assert.equal(transports[0].options.signal.aborted, true)
  assert.equal(transports[0].options.signal.reason.name, 'TimeoutError')
  assert.equal(hook.result.current.compactionProgress.recovery_id, 'saved')
  assert.equal(posts.length, 0)
})

test('reopened pane primary Stop pauses preparation without the turn Stop that clears queued input', async t => {
  // A reopened pane has no local action; only the server read says preparation runs.
  const { hook, props, reads, posts, stops, drafts, failures, turnStops } = setup(t, { serverCompactingKind: 'compact' })
  const saved = { recovery_id: 'saved', next_chunk: 3, total_chunks: 9 }
  reads[0].resolve(response({ progress: { state: 'running', ...saved } }))
  await flush()
  assert.equal(hook.result.current.preparing, true)
  const stop = hook.result.current.stopComposerWork()
  assert.equal(stops.length, 1, 'primary Stop uses preparation-only Pause')
  assert.deepEqual(turnStops, [], 'turn Stop clears queued input and waits for the whole batch')
  stops[0].resolve(response({ ok: true }))
  await stop
  hook.rerender({ ...props, serverCompactingKind: null })
  reads.at(-1).resolve(response({ progress: { state: 'paused', ...saved } }))
  await flush()
  assert.equal(hook.result.current.preparing, false)
  assert.deepEqual(hook.result.current.compactionProgress, { state: 'paused', ...saved }, 'completed sections stay resumable')
  assert.equal(posts.length, 0, 'Pause never starts another synthesis batch')
  assert.deepEqual(drafts, [], 'unsent composer input is untouched')
  assert.deepEqual(failures, [])
  hook.result.current.stopComposerWork()
  assert.deepEqual(turnStops, ['turn'], 'without preparation, primary Stop is the ordinary turn Stop')
  assert.equal(stops.length, 1)
})

test('primary Stop follows the visible chat across A to B to A during a local batch', async t => {
  const { hook, props, posts, stops, turnStops } = setup(t)
  const action = hook.result.current.runCompactCommand('', null)
  assert.equal(hook.result.current.preparing, true)
  hook.rerender({ ...props, chatId: 'b' })
  assert.equal(hook.result.current.preparing, false)
  assert.equal(hook.result.current.compactionProgress, null)
  hook.result.current.stopComposerWork()
  assert.deepEqual(turnStops, ['turn'], 'chat B has no preparation to pause')
  hook.rerender(props)
  assert.equal(hook.result.current.preparing, true)
  assert.deepEqual(hook.result.current.compactionProgress, { state: 'running' })
  void hook.result.current.stopComposerWork()
  assert.equal(stops.length, 1)
  assert.equal(stops[0].chatId, 'a')
  assert.deepEqual(turnStops, ['turn'])
  stops[0].resolve(response({ ok: true }))
  posts[0].resolve(response({ progress: { state: 'paused', recovery_id: 'a-draft' } }))
  await action
  assert.equal(hook.result.current.preparing, false)
})

test('a chatless provisional pane never shows preparation or diverts its Stop', async t => {
  const { hook, props, stops, turnStops } = setup(t)
  hook.rerender({ ...props, chatId: null, provisionalNewChat: true })
  assert.equal(hook.result.current.preparing, false)
  assert.equal(hook.result.current.compactionProgress, null)
  hook.result.current.stopComposerWork()
  assert.deepEqual(turnStops, ['turn'])
  assert.equal(stops.length, 0)
})

test('each chat owns its compact admission: B starts while A POST pends, and A settling cannot release B', async t => {
  const { hook, props, reads, posts, drafts, failures, messages } = setup(t)
  const a = hook.result.current.runCompactCommand('', null)
  const b = { ...props, chatId: 'b' }
  hook.rerender(b)
  reads[1].resolve(response({ progress: { state: 'paused', recovery_id: 'b-saved' } }))
  await flush()
  assert.equal(hook.result.current.preparing, false, 'chat B is not preparing')
  hook.result.current.continueCompaction()
  assert.equal(posts.length, 2, 'chat B Continue is not blocked by chat A')
  assert.equal(posts[1].chatId, 'b')
  assert.equal(posts[1].body.recovery_id, 'b-saved')
  assert.equal(hook.result.current.preparing, true)
  hook.result.current.continueCompaction()
  await hook.result.current.runCompactCommand('', null)
  assert.equal(posts.length, 2, 'same-chat duplicates stay blocked')
  posts[0].reject(new Error('A failed'))
  await a
  assert.equal(hook.result.current.preparing, true, 'old A finally cannot erase B ownership')
  await hook.result.current.runCompactCommand('', null)
  assert.equal(posts.length, 2, 'old A finally cannot admit a duplicate B')
  assert.deepEqual(failures, [null, null], 'each start clears; A failure is not shown in B')
  assert.equal(reads.length, 2, 'departed A cannot read again')
  hook.rerender(props)
  assert.equal(hook.result.current.preparing, false, 'A is free again after its own POST settled')
  const a2 = hook.result.current.runCompactCommand('', null)
  assert.equal(posts.length, 3, 'A can start once its own action settled despite B pending')
  hook.rerender(b)
  assert.equal(hook.result.current.preparing, true)
  posts[1].resolve(response({ ok: true, progress: { state: 'completed' } }))
  await flush()
  assert.equal(hook.result.current.preparing, false, 'B ownership ends with its own POST')
  assert.deepEqual(messages, [], 'a departed B activation leaves refresh to the current activation')
  posts[2].resolve(response({ progress: { state: 'paused' } }))
  await a2
  assert.deepEqual(drafts, [], 'card actions never touch either draft')
})

test('A to B to A keeps a pending A compact fenced against duplicates', async t => {
  const { hook, props, posts } = setup(t)
  const a = hook.result.current.runCompactCommand('', null)
  hook.rerender({ ...props, chatId: 'b' })
  hook.rerender(props)
  await hook.result.current.runCompactCommand('', null)
  assert.equal(posts.length, 1)
  assert.equal(hook.result.current.preparing, true)
  posts[0].resolve(response({ progress: { state: 'paused' } }))
  await a
  assert.equal(hook.result.current.preparing, false)
})

test('each chat owns its Pause admission: B pauses while A Pause pends, and A settling cannot admit a duplicate B', async t => {
  const { hook, props, stops, failures, turnStops } = setup(t, { serverCompactingKind: 'compact' })
  const a = hook.result.current.stopComposerWork()
  assert.equal(stops.length, 1)
  const b = { ...props, chatId: 'b', serverCompactingKind: 'compact' }
  hook.rerender(b)
  const bStop = hook.result.current.stopComposerWork()
  assert.equal(stops.length, 2, 'chat B primary Pause is not blocked by chat A')
  assert.equal(stops[1].chatId, 'b')
  await hook.result.current.stopComposerWork()
  assert.equal(stops.length, 2, 'same-chat Pause duplicate stays blocked')
  stops[0].reject(new Error('A pause failed'))
  await a
  assert.deepEqual(failures, [], 'A failure is not shown in B')
  await hook.result.current.stopComposerWork()
  assert.equal(stops.length, 2, 'old A finally cannot admit a duplicate B Pause')
  hook.rerender({ ...b, chatId: 'a' })
  hook.rerender(b)
  await hook.result.current.stopComposerWork()
  assert.equal(stops.length, 2, 'B to A to B keeps the pending B Pause fenced')
  stops[1].resolve(response({ ok: true }))
  await bStop
  const again = hook.result.current.stopComposerWork()
  assert.equal(stops.length, 3, 'B Pause is admissible again after its own POST settled')
  stops[2].resolve(response({ ok: true }))
  await again
  assert.deepEqual(turnStops, [], 'preparation Stop never becomes the turn Stop')
})

test('B completes in its own activation while A stays pending, and unmount fences A late settlement', async t => {
  const { hook, props, reads, posts, messages, failures } = setup(t)
  const a = hook.result.current.runCompactCommand('', null)
  hook.rerender({ ...props, chatId: 'b' })
  const b = hook.result.current.runCompactCommand('', null)
  assert.deepEqual(posts.map(post => post.chatId), ['a', 'b'])
  posts[1].resolve(response({ ok: true, progress: { state: 'completed' } }))
  await b
  assert.equal(hook.result.current.preparing, false)
  assert.deepEqual(hook.result.current.compactionProgress, { state: 'completed' })
  assert.deepEqual(messages, [{ force: true }], 'visible B refreshes its own transcript')
  assert.equal(reads.at(-1).chatId, 'b', 'B readback targets B')
  const readsBeforeUnmount = reads.length
  hook.unmount()
  posts[0].reject(new Error('A failed late'))
  await a
  await flush()
  assert.equal(reads.length, readsBeforeUnmount, 'unmounted A cannot resurrect a GET')
  assert.deepEqual(failures, [null, null], 'late A failure is not reported')
})


// A submitted command stays in its chat's draft. Only the submitting owner
// pane, still showing its chat, clears it once a finished compaction is
// acknowledged and the draft is unchanged; everywhere else it stays editable.
// Each chat id below is unique because the draft store is module state shared
// by every test in this file.
const rejections = {
  'HTTP 409': post => post.resolve(Response.json({ detail: 'Not admissible now' }, { status: 409 })),
  'HTTP 422': post => post.resolve(Response.json({ detail: 'Invalid instructions' }, { status: 422 })),
  'transport failure': post => post.reject(new TypeError('Failed to fetch')),
}
const completed = post => post.resolve(Response.json({ ok: true, progress: null }))
const attachment = { name: 'plan.pdf', size: 12, mime_type: 'application/pdf', status: 'done' }
const fileNames = files => files.map(file => file.name)

// Mirrors the composer owner's setter: bound to one chat, persisting before state.
function boundDraft(chatId, writes) {
  return {
    chatId,
    setComposerInput: text => {
      writes.push([chatId, text])
      persistComposerDraft(chatId, text, readComposerDraft(chatId).attachments)
    },
  }
}

for (const [name, fail] of Object.entries(rejections)) {
  test(`A to B to A: after a ${name} the custom command is still A's draft, with no retired callback`, async t => {
    const chatA = `retire-a-${name}`, chatB = `retire-b-${name}`
    const submitted = '/compact Preserve the exact owner constraint'
    const writes = []
    persistComposerDraft(chatA, submitted)
    persistComposerDraft(chatB, 'B owner draft')
    const { hook, props, posts, failures } = setup(t, {
      ...boundDraft(chatA, writes), inputValueRef: { current: submitted },
    })
    const action = hook.result.current.runCompactCommand('Preserve the exact owner constraint', submitted)
    hook.rerender({ ...props, ...boundDraft(chatB, writes) })
    hook.rerender(props)
    fail(posts[0])
    await action
    assert.equal(readComposerDraft(chatA).input, submitted, 'the rejected command is never lost')
    assert.equal(readComposerDraft(chatB).input, 'B owner draft', 'nothing crosses into chat B')
    assert.deepEqual(writes, [], 'no pane setter runs, before or after the POST')
    assert.deepEqual(failures, [null], 'the retired activation reports nothing to the current pane')
  })
}

// A real pane: the composer owner and the compaction hook, keyed by chat as
// ChatView is, so leaving a chat unmounts both and reopening mounts fresh ones.
function usePane(props) {
  const draft = useComposerDraftState({ chatId: props.chatId, hidden: props.hidden, inputRef: { current: null } })
  const compact = useCompactCommands({
    ...props,
    inputValueRef: draft.inputValueRef,
    setComposerInput: draft.setComposerInput,
    setSendFailure: draft.setSendFailure,
  })
  return { draft, compact }
}

function mountPane(t, chatId, overrides = {}) {
  const props = {
    chatId, provisionalNewChat: false, hidden: false, serverCompactingKind: null,
    activationSettledRef: { current: true }, fetchMessages: async () => {}, stopTurn: () => {},
    ...overrides,
  }
  const pane = renderHook(usePane, props)
  t.after(() => pane.unmount())
  return { pane, props }
}

// The owner types a custom command into a composer holding one upload and
// submits it the way ChatView does.
function submitFromPane(pane, chatId, submitted) {
  pane.result.current.draft.handleComposerInputChange(submitted)
  const action = pane.result.current.compact.runCompactCommand(submitted.slice('/compact '.length), submitted)
  assert.equal(pane.result.current.draft.input, submitted, 'the pending command stays in the composer')
  assert.equal(readComposerDraft(chatId).input, submitted)
  return action
}

function view(pane) {
  const { input, pendingFiles, sendFailure } = pane.result.current.draft
  return { input, files: fileNames(pendingFiles), sendFailure }
}

for (const [name, fail] of Object.entries(rejections)) {
  test(`a pane reopened before a ${name} already shows the original command and upload`, async t => {
    const chatId = `reopened-${name}`
    const submitted = '/compact Keep the migration plan'
    const { posts } = installApi(t)
    persistComposerDraft(chatId, '', [attachment])
    const { pane } = mountPane(t, chatId)
    const action = submitFromPane(pane, chatId, submitted)
    pane.unmount()
    const { pane: reopened } = mountPane(t, chatId, { serverCompactingKind: 'compact' })
    assert.deepEqual(view(reopened), { input: submitted, files: ['plan.pdf'], sendFailure: null })
    fail(posts[0])
    await action
    assert.deepEqual(view(reopened), { input: submitted, files: ['plan.pdf'], sendFailure: null },
      'no visibility toggle or durable re-read is needed, and no retired error appears')
    assert.equal(readComposerDraft(chatId).input, submitted)
  })
}

test('an edit in the reopened pane is made against the visible original, so it cannot destroy it unknowingly', async t => {
  const chatId = 'reopened-edit'
  const { posts } = installApi(t)
  const { pane } = mountPane(t, chatId)
  const action = submitFromPane(pane, chatId, '/compact Keep owners')
  pane.unmount()
  const { pane: reopened } = mountPane(t, chatId)
  reopened.result.current.draft.handleComposerInputChange('/compact Keep owners and tests')
  posts[0].reject(new TypeError('Failed to fetch'))
  await action
  assert.equal(view(reopened).input, '/compact Keep owners and tests')
  assert.equal(readComposerDraft(chatId).input, '/compact Keep owners and tests')
})

test('unmount then reopen after a failure: the next pane starts with the command and upload', async t => {
  const chatId = 'reopened-after'
  const { posts } = installApi(t)
  persistComposerDraft(chatId, '', [attachment])
  const { pane } = mountPane(t, chatId)
  const action = submitFromPane(pane, chatId, '/compact Keep both')
  pane.unmount()
  posts[0].resolve(Response.json({ detail: 'Not admissible now' }, { status: 409 }))
  await action
  const { pane: reopened } = mountPane(t, chatId)
  assert.deepEqual(view(reopened), { input: '/compact Keep both', files: ['plan.pdf'], sendFailure: null })
})

test('a failure in the pane that still shows the chat reports it and keeps the command', async t => {
  const chatId = 'visible-failure'
  const { posts } = installApi(t)
  const { pane, props } = mountPane(t, chatId)
  const action = submitFromPane(pane, chatId, '/compact Keep visible')
  pane.rerender({ ...props, hidden: true })
  posts[0].resolve(Response.json({ detail: 'Not admissible now' }, { status: 409 }))
  await action
  assert.equal(view(pane).input, '/compact Keep visible')
  assert.match(view(pane).sendFailure, /couldn’t send/)
})

test('a paused partial batch keeps the command; Continue never touches the draft', async t => {
  const chatId = 'paused-batch'
  const { posts } = installApi(t)
  const { pane } = mountPane(t, chatId)
  const action = submitFromPane(pane, chatId, '/compact Keep paused')
  posts[0].resolve(Response.json({ ok: false, progress: { state: 'paused', recovery_id: 'r-1' } }))
  await action
  assert.equal(view(pane).input, '/compact Keep paused')
  assert.equal(pane.result.current.compact.compactionProgress.recovery_id, 'r-1')
  pane.result.current.compact.continueCompaction()
  completed(posts[1])
  await flush()
  assert.equal(posts[1].body.recovery_id, 'r-1')
  assert.equal(view(pane).input, '/compact Keep paused', 'a card action has no draft effect')
})

test('the live submitting owner pane clears exactly the acknowledged command and keeps uploads', async t => {
  const chatId = 'visible-success'
  const { posts } = installApi(t)
  persistComposerDraft(chatId, '', [attachment])
  const { pane } = mountPane(t, chatId)
  const action = submitFromPane(pane, chatId, '/compact Done here')
  completed(posts[0])
  await action
  assert.deepEqual(view(pane), { input: '', files: ['plan.pdf'], sendFailure: null })
  assert.deepEqual(readComposerDraft(chatId), { input: '', attachments: [attachment] })
})

const newerEdits = {
  'text': draft => draft.handleComposerInputChange('newer owner text'),
  'deliberately emptied draft': draft => draft.handleComposerInputChange(''),
  'removed upload': draft => draft.restoreFiles([]),
  'added upload': draft => draft.restoreFiles([attachment, { ...attachment, name: 'notes.md' }]),
}

for (const [name, edit] of Object.entries(newerEdits)) {
  test(`a newer ${name} wins over a finished compaction in the same pane`, async t => {
    const chatId = `fence-live-${name}`
    const { posts } = installApi(t)
    persistComposerDraft(chatId, '', [attachment])
    const { pane } = mountPane(t, chatId)
    const action = submitFromPane(pane, chatId, '/compact Old command')
    edit(pane.result.current.draft)
    const newer = view(pane)
    completed(posts[0])
    await action
    assert.deepEqual(view(pane), newer)
    assert.equal(readComposerDraft(chatId).input, newer.input)
  })
}

test('a pane reopened before success keeps the command editable, saved and visible alike, with no replay', async t => {
  const chatId = 'reopened-before-success'
  const { posts } = installApi(t)
  persistComposerDraft(chatId, '', [attachment])
  const { pane } = mountPane(t, chatId)
  const action = submitFromPane(pane, chatId, '/compact Keep both views')
  pane.unmount()
  const { pane: reopened } = mountPane(t, chatId)
  completed(posts[0])
  await action
  await flush()
  const saved = readComposerDraft(chatId)
  const expected = { input: '/compact Keep both views', files: ['plan.pdf'], sendFailure: null }
  assert.deepEqual(view(reopened), expected)
  assert.deepEqual({ input: saved.input, files: fileNames(saved.attachments), sendFailure: null }, expected)
  assert.equal(posts.length, 1, 'the retained command is never sent again on its own')
})

test('success after retirement retains the command through a later reopen', async t => {
  const chatId = 'retired-success'
  const { posts } = installApi(t)
  persistComposerDraft(chatId, '', [attachment])
  const { pane } = mountPane(t, chatId)
  const action = submitFromPane(pane, chatId, '/compact Finished')
  pane.unmount()
  completed(posts[0])
  await action
  const { pane: reopened } = mountPane(t, chatId)
  await flush()
  assert.deepEqual(view(reopened), { input: '/compact Finished', files: ['plan.pdf'], sendFailure: null })
  assert.equal(posts.length, 1)
})

test('A to B to A: success after the activation retired runs no setter and keeps the command', async t => {
  const chatA = 'retire-success-a', chatB = 'retire-success-b'
  const submitted = '/compact Keep it'
  const writes = []
  persistComposerDraft(chatA, submitted)
  const { hook, props, posts } = setup(t, {
    ...boundDraft(chatA, writes), inputValueRef: { current: submitted },
  })
  const action = hook.result.current.runCompactCommand('Keep it', submitted)
  hook.rerender({ ...props, ...boundDraft(chatB, writes) })
  hook.rerender(props)
  completed(posts[0])
  await action
  assert.deepEqual(writes, [])
  assert.equal(readComposerDraft(chatA).input, submitted)
  assert.equal(readComposerDraft(chatB).input, '')
})

test('card actions leave an unrelated draft alone through success and failure, even when retired', async t => {
  const chatId = 'card-actions'
  const { posts } = installApi(t)
  persistComposerDraft(chatId, 'unrelated owner draft')
  const { pane } = mountPane(t, chatId)
  const start = pane.result.current.compact.runCompactCommand('', null)
  completed(posts[0])
  await start
  const resume = pane.result.current.compact.runCompactCommand('', null, 'recovery-x')
  pane.unmount()
  posts[1].reject(new TypeError('Failed to fetch'))
  await resume
  assert.equal(readComposerDraft(chatId).input, 'unrelated owner draft')
})

// Guest drafts live in grant tab storage and carry no revision, so a
// finished compaction never clears one: an upload-only edit or a same-text
// cycle would look unchanged. The owner workspace a command was submitted from
// must also still be current. Each guest mount settles first: a guest pane's
// async hydration has no revision to defer to a same-tick edit.
function workspaces(t) {
  const previous = { location: globalThis.location, sessionStorage: globalThis.sessionStorage }
  const values = new Map()
  globalThis.sessionStorage = {
    get length() { return values.size },
    key: index => [...values.keys()][index] ?? null,
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  }
  t.after(() => {
    setActiveSharedBrowserGrantId(null)
    Object.assign(globalThis, previous)
  })
  return {
    owner: () => { globalThis.location = { pathname: '/shell/' } },
    guest: () => {
      globalThis.location = { pathname: '/shell/shared' }
      setActiveSharedBrowserGrantId('guest-grant')
    },
  }
}

const guestCases = {
  'an unchanged guest command': {},
  'an upload-only guest edit': { attach: true, edit: draft => draft.restoreFiles([]) },
  'a same-text guest cycle': {
    edit: draft => {
      draft.handleComposerInputChange('something else')
      draft.handleComposerInputChange('/compact Guest command')
    },
  },
  'an owner command acknowledged in a guest workspace': { submitAs: 'owner' },
  'a guest command acknowledged in the owner workspace': { ackAs: 'owner' },
}

for (const [name, { attach, edit, submitAs = 'guest', ackAs = 'guest' }] of Object.entries(guestCases)) {
  test(`${name} stays after a finished compaction`, async t => {
    const chatId = `workspace-${name}`
    const space = workspaces(t)
    space[submitAs]()
    const { posts } = installApi(t)
    if (attach) persistComposerDraft(chatId, '', [attachment])
    const { pane } = mountPane(t, chatId)
    await flush()
    const action = submitFromPane(pane, chatId, '/compact Guest command')
    edit?.(pane.result.current.draft)
    const before = view(pane)
    space[ackAs]()
    completed(posts[0])
    await action
    assert.deepEqual(view(pane), before)
    assert.equal(view(pane).input, '/compact Guest command')
  })
}

test('an owner draft replaced without a revision, as durable hydration does, is not cleared', async t => {
  const inputValueRef = { current: '/compact Submitted' }
  const { hook, posts, drafts } = setup(t, { chatId: 'hydrated-text', inputValueRef })
  const action = hook.result.current.runCompactCommand('Submitted', '/compact Submitted')
  inputValueRef.current = 'hydrated newer durable draft'
  completed(posts[0])
  await action
  assert.deepEqual(drafts, [])
})

test('an owner command restored from tab storage and sent unedited stays when acknowledged as a guest', async t => {
  const chatId = 'workspace-unedited-owner'
  const space = workspaces(t)
  space.owner()
  globalThis.sessionStorage.setItem(`draft:${chatId}`, '/compact Restored')
  const { posts } = installApi(t)
  const { pane } = mountPane(t, chatId)
  await flush()
  assert.equal(view(pane).input, '/compact Restored', 'an unedited restored draft has revision 0')
  const action = pane.result.current.compact.runCompactCommand('Restored', '/compact Restored')
  space.guest()
  completed(posts[0])
  await action
  assert.equal(view(pane).input, '/compact Restored', 'the guest partition is never written')
})
