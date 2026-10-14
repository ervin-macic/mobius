import { test } from 'node:test'
import assert from 'node:assert/strict'
import { acquireAudioSession } from '../audioSession.js'

function navigatorWithSession(type = 'auto') {
  const changes = []
  return {
    changes,
    audioSession: {
      get type() { return type },
      set type(next) { changes.push(next); type = next },
    },
  }
}

test('mounted apps keep playback until the last one leaves', () => {
  const nav = navigatorWithSession()
  const first = acquireAudioSession('playback', nav)
  const second = acquireAudioSession('playback', nav)
  first()
  first()
  assert.equal(nav.audioSession.type, 'playback')
  second()
  assert.deepEqual(nav.changes, ['playback', 'auto'])
})

test('capture wins over app playback and restores it when every capture ends', () => {
  const nav = navigatorWithSession()
  const app = acquireAudioSession('playback', nav)
  const mic = acquireAudioSession('play-and-record', nav)
  const camera = acquireAudioSession('play-and-record', nav)
  mic()
  assert.equal(nav.audioSession.type, 'play-and-record')
  camera()
  assert.equal(nav.audioSession.type, 'playback')
  app()
  assert.deepEqual(nav.changes, ['playback', 'play-and-record', 'playback', 'auto'])
})

test('closing the last app does not interrupt a capture still in progress', () => {
  const nav = navigatorWithSession()
  const app = acquireAudioSession('playback', nav)
  const mic = acquireAudioSession('play-and-record', nav)
  app()
  assert.equal(nav.audioSession.type, 'play-and-record')
  mic()
  assert.equal(nav.audioSession.type, 'auto')
})

test('the host replaces a category it did not choose, such as a stale capture type', () => {
  const nav = navigatorWithSession('play-and-record')
  const app = acquireAudioSession('playback', nav)
  assert.equal(nav.audioSession.type, 'playback')
  app()
})

test('browsers without the audio session API are left untouched', () => {
  const release = acquireAudioSession('playback', {})
  assert.doesNotThrow(release)
})
