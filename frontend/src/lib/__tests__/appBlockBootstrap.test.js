/* Inline presentation context must reach an opted-in app's first render. */
import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'

const frame = readFileSync(new URL('../../../public/app-frame.html', import.meta.url), 'utf8')
const start = frame.indexOf('    function renderMountedComponent()')
const renderSource = frame.slice(start, frame.indexOf('    const OWNED_FONT_FAMILIES', start))

function render({ supportsAppBlocks = true, currentBlockSession = null, currentToken = 'app-session' } = {}) {
  let tree
  const context = {
    mountedRoot: { render: value => { tree = value } },
    mountedComponent: 'App', Fragment: 'Fragment', MountSignal: 'MountSignal',
    _FRAME_APP_ID: 7, currentToken, supportsAppBlocks, currentBlockSession,
    createElement: (type, props, ...children) => ({ type, props, children }),
  }
  runInNewContext(`${renderSource}\nrenderMountedComponent()`, context)
  return tree?.children[1].props
}

test('first render receives passive block context before workspace startup', () => {
  const session = { sessionId: 's', actions: [{ key: 'chat-send:a', label: 'Contribute' }] }
  const props = render({ currentBlockSession: session })
  assert.equal(props.blockSession, session)
  assert.equal(props.appId, 7)
  assert.equal(props.token, 'app-session')
  assert.equal(Object.hasOwn(props, 'initialAction'), false)
})

test('ordinary app mounts and apps without block support keep their existing props', () => {
  assert.equal(render().blockSession, null)
  const props = render({ supportsAppBlocks: false, currentBlockSession: { sessionId: 's' } })
  assert.deepEqual(Object.keys(props).sort(), ['appId', 'token'])
  assert.equal(render({ currentToken: null }), undefined)
})
