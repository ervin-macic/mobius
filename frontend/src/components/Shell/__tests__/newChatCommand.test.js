/* `/new` and `/clear` start a chat beside the source chat, never inside it. */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { copyChatSettings, startChatFromCommand, withCopiedSettings } from '../newChatCommand.js'

const settings = {
  provider: 'claude',
  agentSettings: { model: 'claude-haiku-5-5', effort: 'low' },
}

function deps(overrides = {}) {
  const calls = { createProjectChat: [], newChat: [], notify: [] }
  return {
    calls,
    findProject: id => (id === 'p1' ? { id: 'p1', name: 'Kanban' } : null),
    createProjectChat: async (project, options) => {
      calls.createProjectChat.push({ project, options })
      return { id: 'project-chat' }
    },
    newChat: async options => {
      calls.newChat.push(options)
      return 'new-chat'
    },
    notify: (message, options) => calls.notify.push({ message, options }),
    ...overrides,
  }
}

test('a project chat opens a new chat in that project and sends the typed text there', async () => {
  const d = deps()
  const started = await startChatFromCommand(d, {
    projectId: 'p1', settings, firstMessage: 'Fix the colour dots',
  })
  assert.equal(started, true)
  assert.deepEqual(d.calls.newChat, [])
  assert.equal(d.calls.createProjectChat.length, 1)
  assert.equal(d.calls.createProjectChat[0].project.id, 'p1')
  assert.deepEqual(d.calls.createProjectChat[0].options, {
    prompt: 'Fix the colour dots', autoSend: true, settings,
  })
})

test('without text the new chat opens empty', async () => {
  const d = deps()
  await startChatFromCommand(d, { projectId: 'p1', settings, firstMessage: '' })
  assert.equal(d.calls.createProjectChat[0].options.prompt, null)

  const plain = deps()
  await startChatFromCommand(plain, { settings, firstMessage: '' })
  assert.equal(plain.calls.newChat[0].draft, '')
})

test('a chat outside projects starts through New Chat with the same model', async () => {
  const d = deps()
  const started = await startChatFromCommand(d, { settings, firstMessage: 'hello' })
  assert.equal(started, true)
  assert.deepEqual(d.calls.createProjectChat, [])
  assert.deepEqual(d.calls.newChat, [{
    draft: 'hello', autoSend: true, recordHistory: true, settings,
  }])
})

test('a project the shell cannot find starts no chat rather than one outside it', async () => {
  const d = deps()
  const started = await startChatFromCommand(d, { projectId: 'gone', settings, firstMessage: 'x' })
  assert.equal(started, false)
  assert.deepEqual(d.calls.newChat, [])
  assert.deepEqual(d.calls.createProjectChat, [])
  assert.equal(d.calls.notify.length, 1)
})

test('a failed create reports false so the source chat keeps the typed command', async () => {
  const failing = deps({
    createProjectChat: async () => { throw new Error('offline') },
  })
  assert.equal(await startChatFromCommand(failing, { projectId: 'p1', firstMessage: 'x' }), false)
  assert.equal(failing.calls.notify.length, 1)
  const noChat = deps({ newChat: async () => null })
  assert.equal(await startChatFromCommand(noChat, { firstMessage: 'x' }), false)
})

test('copying settings saves provider and model and hands the reply to the cache', async () => {
  const requests = []
  const saved = []
  const ok = await copyChatSettings({
    updateChat: async (chatId, body) => {
      requests.push({ chatId, body })
      return Response.json({ provider: 'claude', agent_settings_json: body.agent_settings_json })
    },
    onSaved: (chatId, reply) => saved.push({ chatId, reply }),
    notify: () => assert.fail('no notice on success'),
  }, 'new-chat', settings)
  assert.equal(ok.ok, true)
  assert.equal(ok.saved.agent_settings_json.model, 'claude-haiku-5-5')
  assert.deepEqual(requests, [{
    chatId: 'new-chat',
    body: { provider: 'claude', agent_settings_json: settings.agentSettings },
  }])
  assert.equal(saved[0].reply.agent_settings_json.model, 'claude-haiku-5-5')
})

test('a failed settings copy is reported so the first message is not sent', async () => {
  const notices = []
  const rejected = await copyChatSettings({
    updateChat: async () => new Response('{}', { status: 422 }),
    notify: message => notices.push(message),
  }, 'new-chat', settings)
  assert.deepEqual(rejected, { ok: false, saved: null })
  const thrown = await copyChatSettings({
    updateChat: async () => { throw new TypeError('network') },
    notify: message => notices.push(message),
  }, 'new-chat', settings)
  assert.deepEqual(thrown, { ok: false, saved: null })
  assert.equal(notices.length, 2)
})

test('nothing to copy needs no request', async () => {
  const ok = await copyChatSettings({
    updateChat: () => assert.fail('no request without settings'),
    notify: () => assert.fail('no notice'),
  }, 'new-chat', null)
  assert.deepEqual(ok, { ok: true, saved: null })
})

test('the opened chat carries the copied provider and model, not the creation record', () => {
  const created = { id: 'p-1', title: 'New chat', provider: 'codex', agent_settings_json: { model: 'gpt-6' } }
  const saved = { provider: 'claude', agent_settings_json: settings.agentSettings }
  assert.deepEqual(withCopiedSettings(created, saved), {
    ...created, provider: 'claude', agent_settings_json: settings.agentSettings, effective: undefined,
  })
  assert.equal(withCopiedSettings(created, null), created, 'nothing copied leaves the record as created')
})
