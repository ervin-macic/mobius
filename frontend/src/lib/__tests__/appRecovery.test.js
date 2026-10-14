import test from 'node:test'
import assert from 'node:assert/strict'

import {
  appCrashReportDraft,
  appReportChatId,
  findAppStoreApp,
} from '../appRecovery.js'

test('crash report drafts never leave an empty error fence', () => {
  assert.equal(
    appCrashReportDraft('Notes', '   '),
    'The app "Notes" crashed with this error:\n```\nUnknown app error (no message reached the shell)\n```\nPlease investigate and fix.',
  )
  assert.equal(
    appCrashReportDraft('Notes', 'TypeError: unavailable'),
    'The app "Notes" crashed with this error:\n```\nTypeError: unavailable\n```\nPlease investigate and fix.',
  )
})

test('crash reports follow the current app row, not the frame\'s stale chat', () => {
  assert.equal(appReportChatId({ id: 7, chat_id: 'builder' }, 'stale'), 'builder')
  assert.equal(appReportChatId({ id: 7, chat_id: '' }, 'stale'), null)
  assert.equal(appReportChatId({ id: 7, chat_id: null }, 'stale'), null)
  assert.equal(appReportChatId(undefined, 'frame-chat'), 'frame-chat')
  assert.equal(appReportChatId(undefined, ''), null)
})

test('finds the canonical App Store before falling back to its display name', () => {
  const nameFallback = {
    id: 1,
    name: 'App Store',
    manifest_url: 'https://example.test/lookalike',
  }
  const misleadingUrl = {
    id: 2,
    name: 'Lookalike',
    manifest_url: (
      'https://example.test/?next='
      + 'https://raw.githubusercontent.com/mobius-os/app-store/'
    ),
  }
  const canonical = {
    id: 3,
    name: 'Store',
    manifest_url: 'https://raw.githubusercontent.com/mobius-os/app-store/main#manifest-id=store',
  }

  assert.equal(findAppStoreApp([nameFallback, misleadingUrl, canonical]), canonical)
  assert.equal(findAppStoreApp([misleadingUrl, nameFallback]), nameFallback)
  assert.equal(findAppStoreApp(null), null)
})
