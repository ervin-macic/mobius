/* A /btw answer reaches the chat only through Add to chat, and only once. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { api } from '../../../../api/client.js'
import useSideQuestion, { sideQuestionFailure, sideQuestionRetryable } from '../useSideQuestion.js'
import { renderHook } from './react-hook-shim.mjs'

const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve() }
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function setup(t) {
  const asks = [], keeps = [], added = []
  for (const name of ['askSideQuestion', 'keepSideQuestion']) {
    const original = api.chats[name]
    t.after(() => { api.chats[name] = original })
  }
  api.chats.askSideQuestion = (chatId, question, options) => {
    const pending = deferred()
    asks.push({ chatId, question, options, ...pending })
    return pending.promise
  }
  api.chats.keepSideQuestion = (chatId, payload) => {
    const pending = deferred()
    keeps.push({ chatId, payload, ...pending })
    return pending.promise
  }
  const hook = renderHook(useSideQuestion, { chatId: 'chat-a', onAdded: () => added.push('added') })
  t.after(() => hook.unmount())
  return { hook, asks, keeps, added }
}

const answer = { question: 'what runs?', answer: 'The Kanban tests.', provider: 'claude' }

test('an answer shows in the card and nothing is saved until Add to chat', async t => {
  const { hook, asks, keeps } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  assert.equal(hook.result.current.sideQuestion.status, 'asking')
  asks[0].resolve(Response.json(answer))
  await asking
  const card = hook.result.current.sideQuestion
  assert.equal(card.status, 'answered')
  assert.equal(card.answer, 'The Kanban tests.')
  assert.equal(keeps.length, 0)
})

test('Add to chat saves one row with a stable cid and then closes the card', async t => {
  const { hook, asks, keeps, added } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  asks[0].resolve(Response.json(answer))
  await asking
  const adding = hook.result.current.add()
  assert.equal(hook.result.current.sideQuestion.status, 'adding')
  await hook.result.current.add()
  assert.equal(keeps.length, 1, 'a second press while adding sends nothing')
  const { cid, ...rest } = keeps[0].payload
  assert.ok(cid)
  assert.deepEqual(rest, { question: 'what runs?', answer: 'The Kanban tests.', provider: 'claude' })
  keeps[0].resolve(Response.json({ stored: { cid } }))
  await adding
  assert.equal(hook.result.current.sideQuestion, null)
  assert.deepEqual(added, ['added'])
})

test('a failed add keeps the answer and retries with the same cid', async t => {
  const { hook, asks, keeps } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  asks[0].resolve(Response.json(answer))
  await asking
  const first = hook.result.current.add()
  keeps[0].resolve(Response.json(
    { detail: 'Answer the open question first, then add the side question.' },
    { status: 409 },
  ))
  await first
  assert.equal(hook.result.current.sideQuestion.status, 'answered')
  assert.match(hook.result.current.sideQuestion.error, /Answer the open question first/)
  const second = hook.result.current.add()
  assert.equal(keeps[1].payload.cid, keeps[0].payload.cid)
  keeps[1].resolve(Response.json({ stored: {} }))
  await second
  assert.equal(hook.result.current.sideQuestion, null)
})

test('closing while the copy is answering drops its late answer', async t => {
  const { hook, asks } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  hook.result.current.close()
  asks[0].resolve(Response.json(answer))
  await asking
  await flush()
  assert.equal(hook.result.current.sideQuestion, null)
})

test('closing really cancels: the in-flight request is aborted', async t => {
  const { hook, asks } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  assert.equal(asks[0].options.signal.aborted, false)
  hook.result.current.close()
  assert.equal(asks[0].options.signal.aborted, true,
    'close must abort the request so the server can end the hidden fork')
  const abort = new Error('aborted')
  abort.name = 'AbortError'
  asks[0].reject(abort)
  await asking
  await flush()
  assert.equal(hook.result.current.sideQuestion, null)
})

test('switching chats and unmounting abort an in-flight ask', async t => {
  const { hook, asks } = setup(t)
  void hook.result.current.ask('what runs?')
  hook.rerender({ chatId: 'chat-b', onAdded: () => {} })
  assert.equal(asks[0].options.signal.aborted, true)
  void hook.result.current.ask('and here?')
  hook.unmount()
  assert.equal(asks[1].options.signal.aborted, true)
  for (const ask of asks) {
    const abort = new Error('aborted')
    abort.name = 'AbortError'
    ask.reject(abort)
  }
  await flush()
})

test('a successful add still reconciles the transcript after the card closed', async t => {
  const { hook, asks, keeps, added } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  asks[0].resolve(Response.json(answer))
  await asking
  const adding = hook.result.current.add()
  hook.result.current.close()
  keeps[0].resolve(Response.json({ stored: { cid: keeps[0].payload.cid } }))
  await adding
  assert.deepEqual(added, ['added'],
    'the row committed, so hiding it would misreport the write as cancelled')
  assert.equal(hook.result.current.sideQuestion, null)
})

test('an add finishing after a chat switch does not refresh the chat now on screen', async t => {
  const { hook, asks, keeps, added } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  asks[0].resolve(Response.json(answer))
  await asking
  const adding = hook.result.current.add()
  hook.rerender({ chatId: 'chat-b', onAdded: () => added.push('refreshed the chat on screen') })
  keeps[0].resolve(Response.json({ stored: { cid: keeps[0].payload.cid } }))
  await adding
  assert.equal(keeps[0].chatId, 'chat-a', 'the write belongs to the originating chat')
  assert.deepEqual(added, [], 'chat-a reconciles when it reopens, not through chat-b')
})

test('the server says why a question could not be answered, and Try again asks again', async t => {
  const { hook, asks } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  asks[0].resolve(Response.json(
    { detail: 'This chat has nothing to answer from yet. Ask again after its first reply.' },
    { status: 409 },
  ))
  await asking
  assert.equal(hook.result.current.sideQuestion.status, 'failed')
  assert.match(hook.result.current.sideQuestion.error, /nothing to answer from yet/)
  assert.equal(hook.result.current.sideQuestion.retryable, true, 'a first reply clears this refusal')
  hook.result.current.retry()
  assert.equal(asks.length, 2)
  assert.equal(asks[1].question, 'what runs?')
})

test('switching chats drops the card', async t => {
  const { hook, asks } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  hook.rerender({ chatId: 'chat-b', onAdded: () => {} })
  asks[0].resolve(Response.json(answer))
  await asking
  await flush()
  assert.equal(hook.result.current.sideQuestion, null)
})

test('transport failures read as connection or time problems', () => {
  const timeout = new Error('Request timed out')
  timeout.name = 'TimeoutError'
  assert.match(sideQuestionFailure(timeout), /took too long/)
  assert.match(sideQuestionFailure(new TypeError('Failed to fetch')), /Check your connection/)
})

test('a refused request offers no Try again; a later or failed one does', async t => {
  const { hook, asks } = setup(t)
  const asking = hook.result.current.ask('what runs?')
  asks[0].resolve(Response.json(
    { detail: 'Side questions work in Claude chats for now.' }, { status: 422 },
  ))
  await asking
  assert.equal(hook.result.current.sideQuestion.status, 'failed')
  assert.match(hook.result.current.sideQuestion.error, /Claude chats/)
  assert.equal(hook.result.current.sideQuestion.retryable, false,
    'asking the same chat again would be refused the same way')

  const status = code => Object.assign(new Error('x'), { status: code })
  for (const code of [400, 401, 403, 404, 422]) assert.equal(sideQuestionRetryable(status(code)), false, `${code}`)
  for (const code of [408, 409, 429, 500, 502, 504]) assert.equal(sideQuestionRetryable(status(code)), true, `${code}`)
  assert.equal(sideQuestionRetryable(new TypeError('Failed to fetch')), true)
})
