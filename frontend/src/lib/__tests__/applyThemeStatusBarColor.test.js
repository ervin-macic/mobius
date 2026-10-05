import { test } from 'node:test'
import assert from 'node:assert/strict'

import { applyTheme } from '../applyTheme.js'

function fakeDocument() {
  const head = []
  const meta = media => ({
    media,
    attrs: { content: '#000000' },
    getAttribute(name) { return this.attrs[name] },
    setAttribute(name, value) { this.attrs[name] = value },
    remove() { const i = head.indexOf(this); if (i >= 0) head.splice(i, 1) },
  })
  const light = meta('(prefers-color-scheme: light)')
  const dark = meta('(prefers-color-scheme: dark)')
  head.push(light, dark)
  const style = { setProperty() {}, getPropertyValue() { return '' } }
  return {
    head: { appendChild(node) { head.push(node); return node }, children: head },
    body: { style: {} },
    documentElement: { style, setAttribute() {}, getAttribute() { return null } },
    createElement: () => ({ setAttribute() {}, style: {}, dataset: {} }),
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: sel => (sel === 'meta[name="theme-color"]' ? head.filter(n => n.media) : []),
    light, dark, headNodes: head,
  }
}

const store = { getItem: () => null, setItem() {}, removeItem() {} }

test('both scheme theme-color tags follow the active Möbius theme and are re-inserted for iOS', () => {
  const doc = fakeDocument()
  const marker = { media: null }
  doc.headNodes.push(marker)
  applyTheme({ css: ':root{}', bg: '#f0eeeb', mode: 'light' }, { doc, store })
  assert.equal(doc.light.getAttribute('content'), '#f0eeeb')
  assert.equal(doc.dark.getAttribute('content'), '#f0eeeb')
  assert.ok(doc.headNodes.indexOf(doc.light) > doc.headNodes.indexOf(marker))
  assert.ok(doc.headNodes.indexOf(doc.dark) > doc.headNodes.indexOf(marker))
})

test('an unchanged theme colour leaves the tags in place', () => {
  const doc = fakeDocument()
  applyTheme({ css: ':root{}', bg: '#0d0d0d', mode: 'dark' }, { doc, store })
  const marker = { media: null }
  doc.headNodes.push(marker)
  applyTheme({ css: ':root{}', bg: '#0d0d0d', mode: 'dark' }, { doc, store })
  assert.ok(doc.headNodes.indexOf(doc.light) < doc.headNodes.indexOf(marker))
  assert.ok(doc.headNodes.indexOf(doc.dark) < doc.headNodes.indexOf(marker))
})
