import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { Marked } from 'marked'
import { createServer } from 'vite'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { mathTokens } from '../markdown/mathTokens.js'

const vite = await createServer({
  appType: 'custom',
  logLevel: 'error',
  server: { middlewareMode: true, hmr: false, ws: false },
  // Inline markdown can reach the shared image-lightbox UI. Bundle its SDK UI
  // dependency because that package intentionally uses extensionless ESM
  // imports which native Node resolution cannot load during SSR.
  ssr: { noExternal: ['@openai/apps-sdk-ui'] },
})
const { default: InlineContent } = await vite.ssrLoadModule(
  '/src/components/ChatView/markdown/InlineContent.jsx',
)
const { BlockToken } = await vite.ssrLoadModule(
  '/src/components/ChatView/markdown/blocks.jsx',
)
const { StandardMarkdown, ProgressiveMarkdown } = await vite.ssrLoadModule(
  '/src/components/ChatView/markdown/BlockRenderer.jsx',
)

after(() => vite.close())

test('standalone HTML comments render no visible block', () => {
  const md = new Marked()
  const [comment, , paragraph] = md.lexer('<!-- internal note -->\n\nVisible text')

  assert.equal(comment.type, 'html')
  assert.equal(renderToStaticMarkup(React.createElement(BlockToken, { token: comment })), '')
  assert.match(
    renderToStaticMarkup(React.createElement(BlockToken, { token: paragraph })),
    />Visible text<\//,
  )
})

test('escaped currency renders its dollar sign while real math stays math', () => {
  const md = new Marked()
  md.use(mathTokens())
  const [paragraph] = md.lexer('Revenue reached \\$100M while $x$ stays math.')

  assert.deepEqual(
    paragraph.tokens.map(token => [token.type, token.text]),
    [
      ['text', 'Revenue reached '],
      ['escape', '$'],
      ['text', '100M while '],
      ['inlineKatex', 'x'],
      ['text', ' stays math.'],
    ],
  )
  const markup = renderToStaticMarkup(
    React.createElement(InlineContent, { tokens: paragraph.tokens }),
  )
  assert.match(markup, /Revenue reached \$100M while/)
  assert.doesNotMatch(markup, /\\\$100M/)
  assert.match(markup, /md-math-inline/)
})

test('app blocks render only where the caller opts in (assistant replies)', () => {
  const priorWindow = globalThis.window
  globalThis.window = { location: new URL('https://mobius.test/shell') }
  try {
    const fence = '```mobius-app\n' + JSON.stringify({ app: 'contribute', intent: 'review:a', title: 'Fix typo',
      action: { label: 'Contribute', intent: 'chat-send:a' }, pull: { repo: 'owner/repo', state: 'proposed' } }) + '\n```'
    const render = (Component, props) => renderToStaticMarkup(React.createElement(QueryClientProvider,
      { client: new QueryClient() }, React.createElement(Component, { text: fence, ...props })))
    for (const Component of [StandardMarkdown, ProgressiveMarkdown]) {
      const plain = render(Component, {})
      assert.match(plain, /<pre[^]*&quot;app&quot;:&quot;contribute&quot;/)
      assert.doesNotMatch(plain, /md-app-block/)
      const owned = render(Component, { allowAppBlocks: true })
      assert.match(owned, /md-app-block--pull/)
      assert.doesNotMatch(owned, /<pre/)
    }
    // Nested containers inherit the caller's choice.
    const quoted = '> ' + fence.split('\n').join('\n> ')
    assert.doesNotMatch(render(StandardMarkdown, { text: quoted }), /md-app-block/)
    assert.match(render(StandardMarkdown, { text: quoted, allowAppBlocks: true }), /md-app-block/)
  } finally {
    globalThis.window = priorWindow
  }
})
