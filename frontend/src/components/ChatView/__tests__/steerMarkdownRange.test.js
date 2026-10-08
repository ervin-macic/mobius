import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  splitSteerMarkdown, markdownRangeTokens, sliceMarkdownRange,
} from '../markdown/steerMarkdownRange.js'

function visible(tokens) {
  return tokens.map(token => {
    if (token.tokens && (token.type === 'paragraph' || token.type === 'heading'
      || token.type === 'strong' || token.type === 'em' || token.type === 'del')) {
      return visible(token.tokens)
    }
    return token.text ?? ''
  }).join('')
}

test('a steer inside strong keeps formatting and every visible character exactly once', () => {
  const source = '**3. Don’t confuse uncertainty with failure—or success.**'
  const cut = source.indexOf('uncertainty') + 5
  const split = splitSteerMarkdown(source, cut)
  assert.ok(split)
  assert.equal(split.before.source.slice(split.before.start, split.before.end)
    + split.after.source.slice(split.after.start, split.after.end), source)
  assert.equal(visible(markdownRangeTokens(split.before))
    + visible(markdownRangeTokens(split.after)), '3. Don’t confuse uncertainty with failure—or success.')
  assert.equal(markdownRangeTokens(split.before)[0].tokens[0].type, 'strong')
  assert.equal(markdownRangeTokens(split.after)[0].tokens[0].type, 'strong')
  assert.notEqual(markdownRangeTokens(split.before)[0].raw, markdownRangeTokens(split.after)[0].raw)
})

test('nested emphasis and deletion survive projection; unrelated whole blocks stay intact', () => {
  const source = 'Prelude\n\n## A **bold *nested* ~~ending~~** tail'
  const cut = source.indexOf('nested') + 3
  const split = splitSteerMarkdown(source, cut)
  assert.ok(split)
  assert.equal(visible(markdownRangeTokens(split.before))
    + visible(markdownRangeTokens(split.after)), 'PreludeA bold nested ending tail')
  const heading = markdownRangeTokens(split.before).at(-1)
  assert.equal(heading.type, 'heading')
  assert.equal(heading.depth, 2)
  assert.equal(heading.tokens[1].type, 'strong')
  assert.equal(heading.tokens[1].tokens[1].type, 'em')
  assert.equal(markdownRangeTokens(split.after)[0].tokens[0].type, 'strong')
})

test('an unfinished delimiter remains literal text from the full replay', () => {
  const source = 'hello **unfinished'
  const split = splitSteerMarkdown(source, source.indexOf('finished'))
  assert.ok(split)
  assert.equal(visible(markdownRangeTokens(split.before))
    + visible(markdownRangeTokens(split.after)), source)
})

test('fails closed on unsupported crossings and invalid source boundaries', () => {
  for (const source of ['[label](https://example.com)', '`code here`', '$x+y$',
    '<span>hello</span>', '- list item', '| a | b |\n|---|---|\n| c | d |']) {
    assert.equal(splitSteerMarkdown(source, Math.floor(source.length / 2)), null, source)
  }
  assert.equal(splitSteerMarkdown('a👩‍💻b', 2), null)
  assert.equal(splitSteerMarkdown('a &amp; b', 4), null)
  assert.equal(splitSteerMarkdown('plain', 0), null)
  assert.equal(splitSteerMarkdown('plain', 99), null)
})

test('range slicing is relative, immutable, and still projects from original parse', () => {
  const source = '**abcdef**'
  const split = splitSteerMarkdown(source, 5)
  assert.ok(split)
  const original = JSON.stringify(split.after)
  const sliced = sliceMarkdownRange(split.after, 0, 2)
  assert.ok(sliced)
  assert.equal(sliced.start, split.after.start)
  assert.equal(sliced.end, split.after.start + 2)
  assert.equal(visible(markdownRangeTokens(sliced)), 'de')
  assert.equal(markdownRangeTokens(sliced)[0].tokens[0].type, 'strong')
  assert.equal(JSON.stringify(split.after), original)
  assert.equal(sliceMarkdownRange(split.after, -1, 2), null)
  assert.equal(sliceMarkdownRange(split.after, 0, 99), null)
  assert.equal(sliceMarkdownRange(split.after, 3, 4), null)
})
