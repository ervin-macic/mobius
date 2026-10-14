/* The owner's own messages render through the reply formatter, keep what was
   typed, and copy back exactly what was typed. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { createServer } from 'vite'
import { renderWithModels } from './modelRegistryRender.js'

const location = { origin: 'http://localhost', href: 'http://localhost/shell/' }
globalThis.window = { location, innerWidth: 900 }
globalThis.location = location
const vite = await createServer({ appType: 'custom', logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  ssr: { noExternal: ['@openai/apps-sdk-ui', 'dompurify'] },
  // SSR has no DOM; these fixtures need no sanitizer behaviour.
  plugins: [{ name: 'dompurify-ssr-stub', enforce: 'pre',
    resolveId: id => id === 'dompurify' ? '\0dompurify-stub' : null,
    load: id => id === '\0dompurify-stub'
      ? 'export default { sanitize: value => String(value) }' : null }] })
after(() => vite.close())
const { default: Message } = await vite.ssrLoadModule('/src/components/ChatView/MsgContent.jsx')
const { PeerTimelineContext } = await vite.ssrLoadModule('/src/components/ChatView/peerTimelineContext.js')
const { messageCopyText } = await vite.ssrLoadModule('/src/components/ChatView/messageCopy.js')

function render(msg) {
  return renderWithModels(createElement(PeerTimelineContext.Provider,
    { value: { tools: new Map(), positions: new Map() } },
    createElement(Message, { msg, chatId: 'fixture', messageKey: msg.id || 'm' })))
}

/** The complete element that starts at the first `marker`, nested divs included. */
function elementAt(html, marker) {
  const start = html.lastIndexOf('<div', html.indexOf(marker))
  assert.ok(start >= 0, `missing ${marker}`)
  let depth = 0
  for (const match of html.slice(start).matchAll(/<div\b|<\/div>/g)) {
    depth += match[0] === '</div>' ? -1 : 1
    if (depth === 0) return html.slice(start, start + match.index + 6)
  }
  throw new Error(`unbalanced ${marker}`)
}

const formatted = [
  'Two things for the **Kanban** board:',
  '',
  '1. Make the list dots *bigger* on phones',
  '2. Keep the `--accent` colour',
  '',
  '> Phones first.',
  '',
  '| Screen | Dot |',
  '|---|---|',
  '| Phone | 12px |',
  '',
  '```css',
  '.list-dot { width: 12px; }',
  '```',
  '',
  'Area grows by $(12/8)^2$, see [the spec](https://example.com/spec).',
].join('\n')

test('owner messages use the same formatter as replies', () => {
  const owner = elementAt(render({ role: 'user', id: 'u', content: formatted }), 'md-blocks')
  const reply = elementAt(render({ role: 'assistant', id: 'a', content: formatted }), 'md-blocks')
  assert.equal(owner, reply)
  assert.match(owner, /<strong>Kanban<\/strong>/)
  assert.match(owner, /<ol class="md-list"/)
  assert.match(owner, /class="md-inline-code">--accent</)
  assert.match(owner, /<blockquote class="md-blockquote"/)
  assert.match(owner, /<table class="md-table"/)
  assert.match(owner, /class="md-code-block"/)
  assert.match(owner, /href="https:\/\/example\.com\/spec"/)
  assert.doesNotMatch(owner, /\*\*Kanban\*\*|\| Screen \||```/)
})

test('a single typed line break stays a line break in owner messages only', () => {
  const text = 'first line\nsecond line'
  const owner = elementAt(render({ role: 'user', id: 'u', content: text }), 'md-blocks')
  const reply = elementAt(render({ role: 'assistant', id: 'a', content: text }), 'md-blocks')
  assert.match(owner, /first line<br\/>second line/)
  assert.doesNotMatch(reply, /<br\/>/, 'replies keep CommonMark soft-break joining')
})

test('tag-like text the owner typed stays visible instead of being dropped as HTML', () => {
  const text = 'Why does Promise<void> fail?\nat <anonymous>:1:2 <b>not bold</b> <https://example.com>'
  const owner = elementAt(render({ role: 'user', id: 'u', content: text }), 'md-blocks')
  assert.match(owner, /Promise&lt;void&gt; fail\?/)
  assert.match(owner, /at &lt;anonymous&gt;:1:2 &lt;b&gt;not bold&lt;\/b&gt;/)
  assert.match(owner, /href="https:\/\/example\.com"/, 'angle-bracket autolinks still link')
  assert.doesNotMatch(owner, /<b>not bold<\/b>/, 'typed HTML is shown, never rendered')
})

test('goal commands keep their Goal tag instead of Markdown', () => {
  const html = render({ role: 'user', id: 'u', content: '/goal Ship **it**' })
  assert.match(html, /<span class="chat__goal-message-tag" aria-hidden="true">Goal<\/span>/)
  assert.match(html, /<span class="chat__goal-message-objective">Ship \*\*it\*\*<\/span>/)
  assert.doesNotMatch(html, /md-blocks/)
})

test('grouped owner messages stay one bubble with a squiggle between formatted parts', () => {
  const segments = ['First **bold** note', 'Second note:\n- item one\n- item two']
  const html = render({ role: 'user', id: 'u', content: segments.join('\n\n'), segments })
  assert.equal(html.split('chat__text--user').length - 1, 1)
  assert.equal(html.split('chat__batch-squiggle').length - 1, 1)
  assert.match(html, /First <strong>bold<\/strong> note/)
  assert.match(html, /<ul class="md-list"><li class="md-list-item">item one<\/li>/)
  assert.ok(html.indexOf('First') < html.indexOf('chat__batch-squiggle'))
  assert.ok(html.indexOf('chat__batch-squiggle') < html.indexOf('Second note'))
})

test('copying an owner message gives back exactly what was typed', () => {
  const typed = 'Keep **this** and `that`\nas typed.'
  const msg = { role: 'user', id: 'u', content: `${typed}\n\n<app_state>{"open":true}</app_state>` }
  assert.equal(messageCopyText(msg), typed)
  const html = render(msg)
  assert.match(html, /class="chat__markdown-copy-surface"/,
    'a text selection copies back through the Markdown clipboard, like replies')
  assert.match(html, /class="chat__text chat__text--user" data-markdown-block="0"/)
  assert.doesNotMatch(html, /app_state|&quot;open&quot;/)
})
