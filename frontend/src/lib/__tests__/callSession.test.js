import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createCallProvider, MEDIA_CALL } from '../callSession.js'
import { createCallTileLayer } from '../callTileLayer.js'
import { createCapabilityHost } from '../capabilityHost.js'
import { builtInCapabilityProviders } from '../capabilityProviders.js'

const tick = () => new Promise((resolve) => setImmediate(resolve))

async function settle(rounds = 40) {
  for (let round = 0; round < rounds; round += 1) await tick()
}

function domError(name) {
  const error = new Error(name)
  error.name = name
  return error
}

let trackCount = 0
let streamCount = 0

class FakeTrack {
  constructor(kind, { remote = false } = {}) {
    trackCount += 1
    this.id = `${kind}-${trackCount}`
    this.kind = kind
    this.enabled = true
    this.readyState = 'live'
    this.muted = remote
    this.contentHint = ''
    this.level = 0
    this.stops = 0
    this.onended = null
    this.onmute = null
    this.onunmute = null
  }

  stop() {
    this.stops += 1
    this.readyState = 'ended'
  }

  setMuted(muted) {
    this.muted = muted
    ;(muted ? this.onmute : this.onunmute)?.({})
  }

  end() {
    this.readyState = 'ended'
    this.onended?.({})
  }
}

class FakeStream {
  // A remote stream carries the id its sender announced in the SDP msid.
  constructor(tracks = [], id = null) {
    streamCount += 1
    this.id = id ?? `stream-${streamCount}`
    this.tracks = [...tracks]
  }
  getTracks() { return [...this.tracks] }
  getAudioTracks() { return this.tracks.filter((track) => track.kind === 'audio') }
  getVideoTracks() { return this.tracks.filter((track) => track.kind === 'video') }
}

// plan: { audio?: DOMException name, video?: DOMException name } fails that
// kind; `display` fails the screen picker with that name, and `deferDisplay`
// keeps the picker open until the test settles `pickers`. The plan stays
// mutable so a test can change the outcome of the next request.
function fakeDevices(plan = {}) {
  const requests = []
  const granted = []
  const displayRequests = []
  const displays = []
  const pickers = []
  return {
    plan,
    requests,
    granted,
    displayRequests,
    displays,
    pickers,
    async getUserMedia(constraints) {
      requests.push(constraints)
      const tracks = []
      for (const kind of ['audio', 'video']) {
        if (!constraints[kind]) continue
        if (plan[kind]) throw domError(plan[kind])
        tracks.push(new FakeTrack(kind))
      }
      const stream = new FakeStream(tracks)
      granted.push(stream)
      return stream
    },
    getDisplayMedia(constraints) {
      displayRequests.push(constraints)
      if (plan.deferDisplay) {
        return new Promise((resolve, reject) => pickers.push({ resolve, reject }))
      }
      if (plan.display) return Promise.reject(domError(plan.display))
      const stream = new FakeStream([new FakeTrack('video')])
      displays.push(stream)
      return Promise.resolve(stream)
    },
  }
}

function deferredDevices() {
  const calls = []
  return {
    calls,
    getUserMedia(constraints) {
      return new Promise((resolve, reject) => calls.push({ constraints, resolve, reject }))
    },
  }
}

function audioContextClass(contexts, { state = 'running', resumable = true } = {}) {
  return class FakeAudioContext {
    constructor() {
      this.state = state
      this.resumable = resumable
      this.currentTime = 0
      this.nodes = []
      this.destination = { kind: 'destination', inputs: [] }
      this.resumes = 0
      this.onstatechange = null
      contexts.push(this)
    }

    node(kind, extra = {}) {
      const node = {
        kind,
        inputs: [],
        outputs: [],
        disconnected: false,
        ...extra,
        connect(target) {
          this.outputs.push(target)
          target.inputs?.push(this)
          return target
        },
        disconnect() {
          this.disconnected = true
          this.outputs = []
        },
      }
      this.nodes.push(node)
      return node
    }

    createGain() {
      return this.node('gain', {
        gain: { value: 1, setTargetAtTime(value) { this.value = value } },
      })
    }

    createAnalyser() {
      const analyser = this.node('analyser', { fftSize: 2048 })
      analyser.getFloatTimeDomainData = (samples) => {
        const level = analyser.inputs[0]?.stream?.getTracks()[0]?.level ?? 0
        for (let index = 0; index < samples.length; index += 1) {
          samples[index] = index % 2 ? level : -level
        }
      }
      return analyser
    }

    createMediaStreamSource(stream) {
      return this.node('source', { stream })
    }

    resume() {
      this.resumes += 1
      if (this.resumable && this.state === 'suspended') {
        this.state = 'running'
        this.onstatechange?.({})
      }
      return Promise.resolve()
    }

    close() {
      this.state = 'closed'
      return Promise.resolve()
    }
  }
}

function fakeDocument() {
  const created = []
  const document = {
    created,
    createElement(tag) {
      const element = {
        tagName: tag.toUpperCase(),
        ownerDocument: document,
        style: {},
        attributes: {},
        children: [],
        parentNode: null,
        className: '',
        srcObject: null,
        muted: false,
        playing: false,
        setAttribute(name, value) { this.attributes[name] = String(value) },
        appendChild(child) {
          child.parentNode = this
          this.children.push(child)
          return child
        },
        removeChild(child) {
          this.children = this.children.filter((candidate) => candidate !== child)
          child.parentNode = null
          return child
        },
        remove() { this.parentNode?.removeChild(this) },
        play() {
          this.playing = true
          return Promise.resolve()
        },
        pause() { this.playing = false },
      }
      created.push(element)
      return element
    },
  }
  return document
}

function fakeTimers() {
  const intervals = new Map()
  let next = 0
  return {
    setInterval(callback, ms) {
      next += 1
      intervals.set(next, { callback, ms })
      return next
    },
    clearInterval(id) { intervals.delete(id) },
    fire() {
      for (const { callback } of [...intervals.values()]) callback()
    },
    get active() { return [...intervals.values()] },
  }
}

// A small JSEP-shaped model: offers list the m-lines a side sends or already
// receives; an answer can send only on offered m-lines; anything unanswered
// or removed raises negotiationneeded again once stable. Each sender has its
// own m-line, named after its kind for the first one ('video', then
// 'video#2'), and descriptions carry each sending m-line's stream id, as an
// SDP msid does. A removed sender's m-line is never reused, and the far side
// mutes its track once a description stops sending it. Operations are chained
// and asynchronous, so two sides that connect at once genuinely collide.
function fakeRtc() {
  const registry = new Map()
  const instances = []

  class FakeIceCandidate {
    constructor(fields) { Object.assign(this, fields) }
    toJSON() {
      return {
        candidate: this.candidate,
        sdpMid: this.sdpMid,
        sdpMLineIndex: this.sdpMLineIndex,
        usernameFragment: this.usernameFragment,
      }
    }
  }

  class FakeSessionDescription {
    constructor(type, sdp) {
      this.type = type
      this.sdp = sdp
    }
    toJSON() { return { type: this.type, sdp: this.sdp } }
  }

  class FakeDataChannel {
    constructor(pc, label, options = {}) {
      this.pc = pc
      this.label = label
      this.id = options.id
      this.negotiated = options.negotiated
      this.readyState = 'connecting'
      this.sent = []
      this.inbox = []
      this.onopen = null
      this.onmessage = null
    }

    send(data) {
      if (this.readyState !== 'open') throw domError('InvalidStateError')
      this.sent.push(data)
      const target = this.pc.remote?.channels.find((channel) => channel.id === this.id)
      if (target) setImmediate(() => target.deliver(data))
    }

    deliver(data) {
      if (this.readyState === 'open') this.dispatch(data)
      else if (this.readyState === 'connecting') this.inbox.push(data)
    }

    dispatch(data) {
      this.pc.arrivals.push(['state', data])
      this.onmessage?.({ data })
    }

    open() {
      if (this.readyState !== 'connecting') return
      this.readyState = 'open'
      this.onopen?.({})
      for (const data of this.inbox.splice(0)) this.dispatch(data)
    }

    close() { this.readyState = 'closed' }
  }

  class FakePeerConnection {
    constructor(config) {
      this.id = `pc${instances.length + 1}`
      registry.set(this.id, this)
      instances.push(this)
      this.config = config
      this.signalingState = 'stable'
      this.connectionState = 'new'
      this.iceConnectionState = 'new'
      this.localDescription = null
      this.remoteDescription = null
      this.senders = []
      this.kindCounts = {}
      this.channels = []
      this.received = new Map()
      this.negotiated = new Set()
      this.remote = null
      this.remoteUfrag = null
      this.remoteOffer = null
      this.pendingOffer = null
      this.closed = false
      this.generation = 0
      this.pending = 0
      this.chain = Promise.resolve()
      this.checkQueued = false
      this.restartPending = false
      this.rollbacks = 0
      this.offers = 0
      this.restarts = 0
      this.candidates = []
      // What reached this side, in order: state messages and remote tracks.
      this.arrivals = []
      this.onnegotiationneeded = null
      this.onicecandidate = null
      this.ontrack = null
      this.onconnectionstatechange = null
      this.oniceconnectionstatechange = null
    }

    sendKeys() { return this.senders.map(({ key }) => key) }

    sendStreams(keys) {
      return Object.fromEntries(this.senders
        .filter(({ key }) => keys.includes(key))
        .map(({ key, streamId }) => [key, streamId]))
    }

    localItems() {
      return [...this.sendKeys(), ...(this.channels.length ? ['data'] : [])]
    }

    addTrack(track, ...streams) {
      if (this.closed) throw domError('InvalidStateError')
      if (this.senders.some((sender) => sender.track === track)) throw domError('InvalidAccessError')
      const count = (this.kindCounts[track.kind] || 0) + 1
      this.kindCounts[track.kind] = count
      const sender = {
        track,
        key: count === 1 ? track.kind : `${track.kind}#${count}`,
        streamId: streams[0]?.id ?? null,
      }
      this.senders.push(sender)
      this.queueNegotiationCheck()
      return sender
    }

    removeTrack(sender) {
      if (this.closed) throw domError('InvalidStateError')
      if (!this.senders.includes(sender)) return
      this.senders = this.senders.filter((candidate) => candidate !== sender)
      sender.track = null
      this.queueNegotiationCheck()
    }

    createDataChannel(label, options) {
      const channel = new FakeDataChannel(this, label, options)
      this.channels.push(channel)
      this.queueNegotiationCheck()
      return channel
    }

    restartIce() {
      this.restarts += 1
      this.restartPending = true
      this.queueNegotiationCheck()
    }

    queueNegotiationCheck() {
      if (this.checkQueued) return
      this.checkQueued = true
      setImmediate(() => {
        this.checkQueued = false
        if (this.closed || this.pending || this.signalingState !== 'stable') return
        const items = this.localItems()
        const negotiated = items.length === this.negotiated.size
          && items.every((item) => this.negotiated.has(item))
        if (negotiated && !this.restartPending) return
        this.onnegotiationneeded?.({})
      })
    }

    enqueue(operation) {
      this.pending += 1
      const run = this.chain.then(async () => {
        await tick()
        if (this.closed) throw domError('InvalidStateError')
        return operation()
      })
      const done = () => {
        this.pending -= 1
        if (!this.pending && this.signalingState === 'stable') this.queueNegotiationCheck()
      }
      this.chain = run.then(done, done)
      return run
    }

    describe(type, fields) {
      this.generation += 1
      const ufrag = `${this.id}.${this.generation}`
      const sdp = JSON.stringify({ from: this.id, ufrag, ...fields })
      return { ufrag, description: new FakeSessionDescription(type, sdp) }
    }

    gatherCandidates(ufrag) {
      setImmediate(() => {
        if (this.closed) return
        this.onicecandidate?.({
          candidate: new FakeIceCandidate({
            candidate: `candidate:1 1 udp 2122260223 192.0.2.${instances.indexOf(this) + 1} 5000 typ host`,
            sdpMid: '0',
            sdpMLineIndex: 0,
            usernameFragment: ufrag,
          }),
        })
        setImmediate(() => {
          if (!this.closed) this.onicecandidate?.({ candidate: null })
        })
      })
    }

    setLocalDescription(description) {
      return this.enqueue(() => {
        if (description?.type === 'rollback') {
          if (this.signalingState !== 'have-local-offer') throw domError('InvalidStateError')
          this.signalingState = 'stable'
          this.pendingOffer = null
          this.rollbacks += 1
          return
        }
        if (this.signalingState === 'stable' || this.signalingState === 'have-local-offer') {
          const items = this.localItems()
          const mlines = [...new Set([...items, ...this.received.keys()])]
          const send = this.sendKeys()
          const { ufrag, description: offer } = this.describe('offer', {
            send,
            streams: this.sendStreams(send),
            mlines,
          })
          this.localDescription = offer
          this.pendingOffer = items
          this.signalingState = 'have-local-offer'
          this.restartPending = false
          this.offers += 1
          this.gatherCandidates(ufrag)
          return
        }
        if (this.signalingState === 'have-remote-offer') {
          const offered = this.remoteOffer.mlines
          const answered = this.localItems().filter((item) => offered.includes(item))
          const send = this.sendKeys().filter((key) => offered.includes(key))
          const { ufrag, description: answer } = this.describe('answer', {
            send,
            streams: this.sendStreams(send),
            mlines: answered,
          })
          this.localDescription = answer
          this.negotiated = new Set(answered)
          this.signalingState = 'stable'
          this.gatherCandidates(ufrag)
          this.maybeConnect()
          return
        }
        throw domError('InvalidStateError')
      })
    }

    setRemoteDescription(description) {
      return this.enqueue(() => {
        const parsed = JSON.parse(description.sdp)
        if (description.type === 'offer') {
          if (this.signalingState === 'have-local-offer') {
            // Implicit rollback, as current browsers perform for a polite peer.
            this.signalingState = 'stable'
            this.pendingOffer = null
            this.rollbacks += 1
          }
          if (this.signalingState !== 'stable') throw domError('InvalidStateError')
          this.remoteOffer = parsed
          this.signalingState = 'have-remote-offer'
        } else if (description.type === 'answer') {
          if (this.signalingState !== 'have-local-offer') throw domError('InvalidStateError')
          this.negotiated = new Set(this.pendingOffer)
          this.pendingOffer = null
          this.signalingState = 'stable'
        } else {
          throw domError('TypeError')
        }
        this.remoteDescription = new FakeSessionDescription(description.type, description.sdp)
        this.remoteUfrag = parsed.ufrag
        this.remote = registry.get(parsed.from) || null
        for (const key of parsed.send) this.receive(key, parsed.streams?.[key])
        // A sender the far side removed goes quiet: browsers mute its track.
        for (const [key, track] of this.received) {
          if (!parsed.send.includes(key) && !track.muted) track.setMuted(true)
        }
        if (this.signalingState === 'stable') this.maybeConnect()
      })
    }

    addIceCandidate(candidate) {
      return this.enqueue(() => {
        if (!this.remoteDescription) throw domError('InvalidStateError')
        if (candidate?.usernameFragment && candidate.usernameFragment !== this.remoteUfrag) {
          throw domError('OperationError')
        }
        this.candidates.push(candidate)
      })
    }

    // Test hook: remote media for m-line `key` ('audio', 'video', 'video#2')
    // arrives on this connection, in the sender's stream `streamId`.
    receive(key, streamId = null) {
      if (this.received.has(key)) return this.received.get(key)
      const track = new FakeTrack(key.split('#')[0], { remote: true })
      this.received.set(key, track)
      this.arrivals.push(['track', key])
      this.ontrack?.({ track, streams: [new FakeStream([track], streamId)] })
      if (this.connectionState === 'connected') setImmediate(() => track.setMuted(false))
      return track
    }

    maybeConnect() {
      if (this.connectionState === 'connected') return
      if (!this.localDescription || !this.remoteDescription) return
      setImmediate(() => this.simulateConnected())
    }

    // Test hook: ICE and DTLS completed.
    simulateConnected() {
      if (this.closed || this.connectionState === 'connected') return
      this.connectionState = 'connected'
      this.iceConnectionState = 'connected'
      this.oniceconnectionstatechange?.({})
      this.onconnectionstatechange?.({})
      for (const channel of this.channels) channel.open()
      for (const track of this.received.values()) {
        if (track.muted) track.setMuted(false)
      }
    }

    close() {
      this.closed = true
      this.signalingState = 'closed'
      this.connectionState = 'closed'
      this.iceConnectionState = 'closed'
      for (const channel of this.channels) channel.close()
      for (const track of this.received.values()) track.readyState = 'ended'
    }
  }

  return { FakePeerConnection, instances }
}

function environment({
  devices = fakeDevices(),
  rtc = fakeRtc(),
  audio = {},
} = {}) {
  const document = fakeDocument()
  const timers = fakeTimers()
  const contexts = []
  const layer = document.createElement('div')
  let clock = 1_000
  return {
    devices,
    rtc,
    document,
    timers,
    contexts,
    layer,
    advance(ms) { clock += ms },
    deps: {
      mediaDevices: devices,
      RTCPeerConnectionCtor: rtc.FakePeerConnection,
      MediaStreamCtor: FakeStream,
      AudioContextCtor: audioContextClass(contexts, audio),
      createElement: (tag) => document.createElement(tag),
      createSurface: () => createCallTileLayer({ getContainer: () => layer }),
      now: () => clock,
      setInterval: timers.setInterval,
      clearInterval: timers.clearInterval,
    },
  }
}

function recorder() {
  const log = []
  const listeners = new Map()
  let resolveReady
  let rejectReady
  let resolveResult
  let rejectResult
  const ready = new Promise((resolve, reject) => {
    resolveReady = resolve
    rejectReady = reject
  })
  const result = new Promise((resolve, reject) => {
    resolveResult = resolve
    rejectResult = reject
  })
  ready.catch(() => {})
  result.catch(() => {})
  return {
    log,
    ready,
    result,
    channel: {
      ready(value) {
        log.push(['ready', value])
        resolveReady(value)
      },
      event(name, value) {
        log.push([name, value])
        for (const listener of listeners.get(name) || []) listener(value)
      },
      result(value) {
        log.push(['result', value])
        resolveReady(undefined)
        resolveResult(value)
      },
      error(error) {
        log.push(['failure', error])
        rejectReady(error)
        rejectResult(error)
      },
    },
    on(name, listener) {
      if (!listeners.has(name)) listeners.set(name, [])
      listeners.get(name).push(listener)
    },
    events(name) {
      return log.filter(([entry]) => entry === name).map(([, value]) => value)
    },
  }
}

function openCall(env, input = {}, limits = { max_peers: 8 }) {
  const provider = createCallProvider(env.deps)
  const session = recorder()
  const handle = provider.open({
    input,
    declaration: { version: 1, limits },
    channel: session.channel,
  })
  session.control = (action, value) => handle.control(action, value)
  return session
}

// Everything the app can observe must be plain JSON: no stream, track,
// connection, description, candidate, or element object, and no function.
function assertPlainJson(value, path = 'value') {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return
  if (typeof value === 'number') {
    assert.ok(Number.isFinite(value), `${path} is a finite number`)
    return
  }
  assert.equal(typeof value, 'object', `${path} is JSON`)
  const prototype = Object.getPrototypeOf(value)
  assert.ok(
    prototype === Object.prototype || prototype === Array.prototype,
    `${path} is a plain object or array`,
  )
  for (const [key, child] of Object.entries(value)) assertPlainJson(child, `${path}.${key}`)
}

function assertAppSawOnlyJson(session) {
  for (const [kind, value] of session.log) {
    if (kind === 'failure') continue
    assertPlainJson(value, kind)
    assert.deepEqual(JSON.parse(JSON.stringify(value)), value)
  }
}

// The app relays its own signals unchanged, over its own JSON transport.
function relay(from, fromName, to, toName) {
  from.on('signal', ({ peer, data }) => {
    if (peer !== toName) return
    const wire = JSON.parse(JSON.stringify(data))
    setImmediate(() => to.control('signal', { peer: fromName, data: wire }))
  })
}

// Painted tiles in visual (stacking) order, bottom first.
function paintedTiles(env) {
  return [...env.layer.children]
    .sort((left, right) => Number(left.style.zIndex) - Number(right.style.zIndex))
    .map((tile) => ({
      track: tile.children[0].srcObject?.getTracks()[0],
      style: tile.style,
      video: tile.children[0],
    }))
}

// A reviewed declaration that may share the screen.
const SHARING = { max_peers: 8, screen_share: 1 }

// Alice and Bob, both with a microphone and camera, connected and relaying.
async function connectedPair() {
  const rtc = fakeRtc()
  const aliceEnv = environment({ rtc })
  const bobEnv = environment({ rtc })
  const alice = openCall(aliceEnv, { audio: true, video: true }, SHARING)
  const bob = openCall(bobEnv, { audio: true, video: true }, SHARING)
  await alice.ready
  await bob.ready
  relay(alice, 'alice', bob, 'bob')
  relay(bob, 'bob', alice, 'alice')
  alice.control('connect', { peer: 'bob', polite: false })
  bob.control('connect', { peer: 'alice', polite: true })
  await settle(80)
  const [alicePc, bobPc] = rtc.instances
  return { aliceEnv, bobEnv, alice, bob, alicePc, bobPc }
}

// Bob paints Alice's camera, then her shared screen above it.
const ALICE_TILES = {
  tiles: [
    { peer: 'alice', x: 0, y: 0, width: 96, height: 72 },
    { peer: 'alice', source: 'screen', x: 100, y: 0, width: 320, height: 180 },
  ],
}

test('media.call validates its input before touching any device', () => {
  const env = environment()
  const provider = createCallProvider(env.deps)
  const channel = { ready() {}, event() {}, result() {}, error() {} }
  const declaration = { version: 1, limits: { max_peers: 8 } }
  const invalidInputs = [
    { camera: true },
    { audio: 'yes' },
    { video: 1 },
    { iceServers: 'stun:stun.example.org' },
    { iceServers: Array.from({ length: 5 }, () => ({ urls: 'stun:stun.example.org' })) },
    { iceServers: [{ urls: 'https://stun.example.org' }] },
    { iceServers: [{ urls: [] }] },
    { iceServers: [{ urls: ['stun:a', 'stun:b', 'stun:c', 'stun:d', 'stun:e'] }] },
    { iceServers: [{ urls: `stun:${'x'.repeat(600)}` }] },
    { iceServers: [{ urls: 'stun:' }] },
    { iceServers: [{ urls: 'turn:turn.example.org', username: 'only-user' }] },
    { iceServers: [{ urls: 'stun:s.example', username: 'u', credential: 'x'.repeat(257) }] },
    { iceServers: [{ urls: 'stun:s.example', credentialType: 'oauth' }] },
    { iceServers: [null] },
  ]
  for (const input of invalidInputs) {
    assert.throws(
      () => provider.open({ input, declaration, channel }),
      (error) => error.code === 'invalid_request' && error.name === 'TypeError',
      JSON.stringify(input),
    )
  }
  assert.deepEqual(env.devices.requests, [])
  assert.equal(env.contexts.length, 0)

  const session = openCall(env, {
    audio: true,
    video: true,
    iceServers: [
      { urls: 'stun:stun.l.google.com:19302' },
      { urls: ['turn:turn.example.org:3478', 'turns:turn.example.org:443'], username: 'u', credential: 'c' },
    ],
  })
  assert.equal(typeof session.control, 'function')
})

test('a receive-only participant opens no devices but still plays audio', async () => {
  const env = environment()
  const session = openCall(env, { audio: false, video: false })

  assert.deepEqual(await session.ready, { audio: false, video: false, playback: 'running' })
  assert.deepEqual(env.devices.requests, [])
  assert.equal(env.contexts.length, 1)

  session.control('connect', { peer: 'host', polite: true })
  await settle()
  const pc = env.rtc.instances[0]
  assert.deepEqual(pc.senders, [], 'nothing is sent without local devices')
  pc.receive('audio')
  pc.simulateConnected()
  await settle(4)
  assert.deepEqual(session.events('peer').at(-1), {
    peer: 'host', state: 'connected', audio: true, video: false, screen: false,
  })
  assertAppSawOnlyJson(session)
})

test('a camera that is missing, busy, or refused leaves an audio-only call', async () => {
  for (const [failure, code] of [['NotReadableError', 'unavailable'], ['NotAllowedError', 'denied']]) {
    const env = environment({ devices: fakeDevices({ video: failure }) })
    const session = openCall(env, { audio: true, video: true })

    assert.deepEqual(await session.ready, {
      audio: true, video: false, playback: 'running', videoError: code,
    })
    assert.equal(env.devices.requests.length, 2)
    assert.ok(env.devices.requests[0].video)
    assert.equal(env.devices.requests[1].video, false)
    assert.equal(env.devices.requests[1].audio.echoCancellation, true)
  }
})

test('a missing microphone still joins with the camera, but a refusal is respected', async () => {
  const missing = environment({ devices: fakeDevices({ audio: 'NotFoundError' }) })
  const camera = openCall(missing, { audio: true, video: true })
  assert.deepEqual(await camera.ready, {
    audio: false, video: true, playback: 'running', audioError: 'unavailable',
  })
  assert.equal(missing.devices.requests.length, 3)

  const refused = environment({ devices: fakeDevices({ audio: 'NotAllowedError' }) })
  const declined = openCall(refused, { audio: true, video: true })
  await assert.rejects(declined.ready, { code: 'denied', name: 'NotAllowedError' })
  assert.equal(refused.devices.requests.length, 2, 'no further prompt after a refusal')
  assert.equal(refused.contexts[0].state, 'closed')
})

test('a call that obtains nothing it asked for fails with stable codes', async () => {
  const denied = environment({ devices: fakeDevices({ audio: 'NotAllowedError' }) })
  const refused = openCall(denied, { audio: true })
  await assert.rejects(refused.result, { code: 'denied' })
  assert.equal(denied.contexts[0].state, 'closed')
  assert.equal(denied.timers.active.length, 0)

  const missing = environment({
    devices: fakeDevices({ audio: 'NotFoundError', video: 'NotFoundError' }),
  })
  const absent = openCall(missing, { audio: true, video: true })
  await assert.rejects(absent.ready, { code: 'unavailable', name: 'NotFoundError' })
  assert.equal(missing.devices.requests.length, 3)

  const unsupported = environment()
  unsupported.deps.RTCPeerConnectionCtor = undefined
  assert.throws(
    () => openCall(unsupported, {}),
    { code: 'unavailable' },
  )
})

test('cancelling during the permission prompt settles at once and stops late tracks', async () => {
  const devices = deferredDevices()
  const env = environment({ devices })
  const session = openCall(env, { audio: true, video: true })
  await settle(2)
  assert.equal(devices.calls.length, 1)

  session.control('connect', { peer: 'early', polite: false })
  session.control('cancel')
  await assert.rejects(session.ready, { code: 'aborted', name: 'AbortError' })
  await assert.rejects(session.result, { code: 'aborted' })

  const late = new FakeStream([new FakeTrack('audio'), new FakeTrack('video')])
  devices.calls[0].resolve(late)
  await settle(4)
  assert.deepEqual(late.getTracks().map((track) => track.stops), [1, 1])
  assert.equal(env.rtc.instances.length, 0, 'queued connection work is dropped')
  assert.equal(env.contexts[0].state, 'closed')
  assert.equal(session.events('ready').length, 0)
})

test('perfect negotiation resolves offer glare between two shells', async () => {
  const rtc = fakeRtc()
  const aliceEnv = environment({ rtc })
  const bobEnv = environment({ rtc })
  const alice = openCall(aliceEnv, { audio: true, video: true })
  const bob = openCall(bobEnv, { audio: true, video: true })
  await alice.ready
  await bob.ready
  relay(alice, 'alice', bob, 'bob')
  relay(bob, 'bob', alice, 'alice')

  // Both players walk into range in the same instant.
  alice.control('connect', { peer: 'bob', polite: false })
  bob.control('connect', { peer: 'alice', polite: true })
  await settle(80)

  const [alicePc, bobPc] = rtc.instances
  const offers = (session) => session.events('signal')
    .filter(({ data }) => data.description?.type === 'offer')
  assert.equal(offers(alice).length, 1)
  assert.equal(offers(bob).length, 1, 'both sides offered: a real collision')
  assert.equal(bobPc.rollbacks, 1, 'the polite side rolled its offer back')
  assert.equal(alicePc.rollbacks, 0, 'the impolite side ignored the colliding offer')
  assert.equal(alicePc.remoteDescription.type, 'answer')
  assert.equal(alicePc.signalingState, 'stable')
  assert.equal(bobPc.signalingState, 'stable')
  assert.deepEqual(alice.events('error'), [])
  assert.deepEqual(bob.events('error'), [])
  assert.deepEqual(alice.events('peer').at(-1), {
    peer: 'bob', state: 'connected', audio: true, video: true, screen: false,
  })
  assert.deepEqual(bob.events('peer').at(-1), {
    peer: 'alice', state: 'connected', audio: true, video: true, screen: false,
  })
  assert.ok(alice.events('signal').some(({ data }) => data.candidate === null))
  assert.ok(bobPc.candidates.length >= 1)

  // Alice paints Bob over his avatar and herself in a corner.
  alice.control('tiles', {
    tiles: [
      { peer: 'bob', x: 120, y: 80, width: 96, height: 72, radius: 12 },
      { peer: 'self', x: 8, y: 8, width: 64, height: 48 },
    ],
  })
  const tiles = paintedTiles(aliceEnv)
  assert.equal(tiles.length, 2)
  assert.equal(tiles[0].track, alicePc.received.get('video'))
  assert.equal(tiles[0].video.style.transform, '')
  assert.equal(tiles[1].track.kind, 'video')
  assert.equal(tiles[1].track.remote, undefined)
  assert.equal(tiles[1].video.style.transform, 'scaleX(-1)', 'self view mirrors by default')

  // Bob turns his camera off: Alice's tile disappears without renegotiation.
  const offersBefore = alicePc.offers + bobPc.offers
  bob.control('local', { video: false })
  await settle(6)
  assert.deepEqual(bob.events('local').at(-1), { audio: true, video: false, screen: false })
  assert.deepEqual(alice.events('peer').at(-1), {
    peer: 'bob', state: 'connected', audio: true, video: false, screen: false,
  })
  assert.equal(paintedTiles(aliceEnv).length, 1)
  assert.equal(paintedTiles(aliceEnv)[0].track.kind, 'video')
  assert.equal(alicePc.offers + bobPc.offers, offersBefore)

  // Speaking levels follow the remote audio.
  alicePc.received.get('audio').level = 0.1
  aliceEnv.timers.fire()
  const levels = alice.events('levels').at(-1)
  assert.ok(levels.peers.bob > 0 && levels.peers.bob <= 1)
  assert.equal(levels.self, 0)

  assertAppSawOnlyJson(alice)
  assertAppSawOnlyJson(bob)
})

test('an offer opens a polite connection, and stale payloads are ignored', async () => {
  const rtc = fakeRtc()
  const hostEnv = environment({ rtc })
  const guestEnv = environment({ rtc })
  const host = openCall(hostEnv, { audio: true })
  const guest = openCall(guestEnv, { audio: true })
  await host.ready
  await guest.ready
  relay(host, 'host', guest, 'guest')
  relay(guest, 'guest', host, 'host')

  // Only the host connects; the guest learns of the call from its offer.
  host.control('connect', { peer: 'guest', polite: false })
  await settle(60)
  assert.equal(rtc.instances.length, 2)
  assert.deepEqual(guest.events('peer').at(-1), {
    peer: 'host', state: 'connected', audio: true, video: false, screen: false,
  })
  guest.control('connect', { peer: 'host', polite: true })
  await settle(4)
  assert.equal(rtc.instances.length, 2, 'connect is idempotent')

  guest.control('signal', {
    peer: 'stranger',
    data: { candidate: { candidate: 'candidate:9 1 udp 1 192.0.2.9 9 typ host', sdpMid: '0' } },
  })
  guest.control('signal', {
    peer: 'stranger',
    data: { description: { type: 'answer', sdp: 'v=0' } },
  })
  await settle(4)
  assert.equal(rtc.instances.length, 2)
  assert.deepEqual(guest.events('error'), [])

  host.control('disconnect', { peer: 'guest' })
  host.control('disconnect', { peer: 'guest' })
  await settle(4)
  assert.equal(rtc.instances[0].closed, true)
  assert.deepEqual(
    host.events('peer').filter(({ state }) => state === 'closed'),
    [{ peer: 'guest', state: 'closed', audio: false, video: false, screen: false }],
  )
  assertAppSawOnlyJson(host)
  assertAppSawOnlyJson(guest)
})

test('max_peers turns extra peers into non-fatal error events', async () => {
  const env = environment()
  const session = openCall(env, { audio: true }, { max_peers: 2 })
  await session.ready

  session.control('connect', { peer: 'p1', polite: true })
  session.control('connect', { peer: 'p2', polite: true })
  session.control('connect', { peer: 'p3', polite: true })
  session.control('signal', {
    peer: 'p4',
    data: { description: { type: 'offer', sdp: 'v=0' } },
  })
  await settle()

  assert.equal(env.rtc.instances.length, 2)
  assert.deepEqual(
    session.events('error').map(({ peer, code }) => [peer, code]),
    [['p3', 'limit_exceeded'], ['p4', 'limit_exceeded']],
  )
  session.control('disconnect', { peer: 'p1' })
  session.control('connect', { peer: 'p3', polite: true })
  await settle()
  assert.equal(env.rtc.instances.length, 3, 'a freed slot can be reused')
  assert.equal(session.log.some(([kind]) => kind === 'failure'), false)
})

test('invalid controls report invalid_request without ending the call', async () => {
  const env = environment()
  const session = openCall(env, { audio: true, video: true }, { max_peers: 2 })
  await session.ready
  session.control('connect', { peer: 'p1', polite: false })
  await settle()

  const attempts = [
    ['connect', { peer: 'bad id!', polite: true }],
    ['connect', { peer: 'self', polite: true }],
    ['connect', { peer: 'p2' }],
    ['connect', { peer: 'p2', polite: true, video: true }],
    ['signal', { peer: 'p1', data: { description: { type: 'offer', sdp: 'x'.repeat(100 * 1024 + 1) } } }],
    ['signal', { peer: 'p1', data: { description: { type: 'rollback', sdp: 'v=0' } } }],
    ['signal', { peer: 'p1', data: { candidate: { candidate: 'x'.repeat(2049), sdpMid: '0' } } }],
    ['signal', { peer: 'p1', data: { candidate: { candidate: 'c', sdpMLineIndex: -1 } } }],
    ['signal', { peer: 'p1', data: { candidate: null, description: null } }],
    ['signal', { peer: 'p1', data: 'offer' }],
    ['disconnect', { peer: 7 }],
    ['volume', { gains: [] }],
    ['tiles', { tiles: [{ peer: 'p1', x: Number.NaN, y: 0, width: 10, height: 10 }] }],
    ['tiles', { tiles: [{ peer: 'p1', x: 0, y: 0, width: 0, height: 10 }] }],
    ['tiles', { tiles: [{ peer: 'p1', x: 0, y: 0, width: 10, height: 10, zIndex: 9 }] }],
    ['tiles', { tiles: 'all' }],
    ['tiles', { tiles: [{ peer: 'p1', source: 'window', x: 0, y: 0, width: 10, height: 10 }] }],
    ['local', { audio: 'off' }],
    ['local', { screen: true }],
    ['screen', { share: 'yes' }],
    ['screen', { share: true, audio: true }],
    ['screen', null],
    ['explode', {}],
  ]
  for (const [action, value] of attempts) session.control(action, value)
  await settle()

  const errors = session.events('error')
  assert.equal(errors.length, attempts.length)
  assert.ok(errors.every(({ code }) => code === 'invalid_request'))
  assert.equal(errors[0].peer, null, 'an invalid id is never echoed back as a peer')
  assert.equal(errors[4].peer, 'p1')

  const answer = {
    peer: 'p1',
    data: { description: { type: 'answer', sdp: '{"from":"nobody","ufrag":"x","send":[]}' } },
  }
  session.control('signal', answer)
  await settle()
  assert.equal(session.events('error').length, attempts.length, 'p1 had asked for this answer')
  session.control('signal', answer)
  await settle()
  // The browser rejects a second, unsolicited answer; still not fatal.
  assert.deepEqual(
    session.events('error').slice(attempts.length).map(({ peer, code }) => [peer, code]),
    [['p1', 'invalid_request']],
  )

  env.advance(1_500)
  session.control('finish')
  assert.deepEqual(await session.result, { durationMs: 1_500 })
})

test('volume clamps per-peer gains, including gains set before audio arrives', async () => {
  const env = environment()
  const session = openCall(env, { audio: true })
  await session.ready
  const context = env.contexts[0]

  session.control('volume', { gains: { p2: 0.6 } })
  session.control('connect', { peer: 'p1', polite: true })
  session.control('connect', { peer: 'p2', polite: true })
  session.control('volume', { gains: { p1: 0.25 } })
  await settle(4)
  const [pc1, pc2] = env.rtc.instances
  const remote1 = pc1.receive('audio')
  pc2.receive('audio')

  const gains = context.nodes.filter((node) => node.kind === 'gain')
  assert.equal(gains.length, 2)
  assert.equal(gains[0].gain.value, 0.25)
  assert.equal(gains[1].gain.value, 0.6)
  assert.ok(gains.every((gain) => gain.outputs[0] === context.destination))
  const sink = env.document.created.find((element) => element.tagName === 'AUDIO')
  assert.equal(sink.muted, true, 'the Chrome remote-stream sink never plays sound')
  assert.equal(sink.srcObject.getTracks()[0], remote1)

  session.control('volume', { gains: { p1: 7, p2: -3 } })
  assert.equal(gains[0].gain.value, 1)
  assert.equal(gains[1].gain.value, 0)
  session.control('volume', { gains: { p1: 'loud', p2: Number.NaN, 'not valid!': 0.5 } })
  assert.equal(gains[0].gain.value, 1)
  assert.deepEqual(
    session.events('error').map(({ peer, code }) => [peer, code]),
    [['p1', 'invalid_request'], ['p2', 'invalid_request'], [null, 'invalid_request']],
  )
  assert.deepEqual(session.events('playback'), [])
})

test('tiles are validated, replaced wholesale, and paint only live video', async () => {
  const env = environment()
  const session = openCall(env, { audio: true, video: true }, { max_peers: 2 })
  await session.ready
  session.control('connect', { peer: 'p1', polite: true })
  await settle(4)
  const pc = env.rtc.instances[0]

  session.control('tiles', {
    tiles: [
      { peer: 'p1', x: 40, y: 50, width: 120, height: 90, radius: -4, opacity: 3 },
      { peer: 'self', x: -10, y: 5, width: 80, height: 60, mirror: false, opacity: 0.5 },
    ],
  })
  let tiles = paintedTiles(env)
  assert.equal(tiles.length, 1, 'p1 has no live video yet; its placeholder stays visible')
  assert.equal(tiles[0].video.style.transform, '')
  assert.equal(tiles[0].style.opacity, '0.5')
  assert.equal(tiles[0].style.left, '-10px')
  assert.equal(tiles[0].style.pointerEvents, 'none')
  assert.equal(tiles[0].video.style.pointerEvents, 'none')
  assert.equal(tiles[0].video.muted, true)

  const remoteVideo = pc.receive('video')
  pc.simulateConnected()
  await settle(4)
  tiles = paintedTiles(env)
  assert.equal(tiles.length, 2, 'video arriving repaints without a new tiles control')
  assert.equal(tiles[0].track, remoteVideo)
  assert.equal(tiles[0].style.borderRadius, '0px')
  assert.equal(tiles[0].style.opacity, '1')
  assert.equal(tiles[0].style.width, '120px')
  const firstElement = env.layer.children.find((element) => (
    element.children[0].srcObject?.getTracks()[0] === remoteVideo
  ))

  session.control('tiles', {
    tiles: [{ peer: 'p1', x: 60, y: 70, width: 120, height: 90 }],
  })
  assert.equal(env.layer.children.length, 1)
  assert.equal(env.layer.children[0], firstElement, 'moving a tile reuses its element')
  assert.equal(firstElement.style.left, '60px')

  // Every participant's camera and screen: 2 * (max_peers + 1) tiles at most.
  const tooMany = Array.from({ length: 7 }, () => ({ peer: 'self', x: 0, y: 0, width: 1, height: 1 }))
  session.control('tiles', { tiles: tooMany })
  assert.equal(session.events('error').at(-1).code, 'limit_exceeded')
  assert.equal(env.layer.children.length, 1, 'a rejected update keeps the previous tiles')

  // The remote's own camera-off announcement hides the tile.
  pc.channels[0].deliver(JSON.stringify({ audio: true, video: false }))
  assert.equal(env.layer.children.length, 0)
  pc.channels[0].deliver(JSON.stringify({ audio: true, video: true }))
  assert.equal(env.layer.children.length, 1)
  remoteVideo.setMuted(true)
  assert.equal(env.layer.children.length, 0)
  remoteVideo.setMuted(false)

  session.control('tiles', { tiles: [] })
  assert.equal(env.layer.children.length, 0)
  assert.equal(firstElement.children[0].srcObject, null)
  assertAppSawOnlyJson(session)
})

test('local mute toggles tracks without renegotiation and follows device loss', async () => {
  const env = environment()
  const session = openCall(env, { audio: true, video: true })
  await session.ready
  session.control('connect', { peer: 'p1', polite: false })
  await settle(6)
  const pc = env.rtc.instances[0]
  pc.simulateConnected()
  await settle(2)
  const [stream] = env.devices.granted
  const [audio] = stream.getAudioTracks()
  const [video] = stream.getVideoTracks()
  session.control('tiles', { tiles: [{ peer: 'self', x: 0, y: 0, width: 64, height: 48 }] })
  assert.equal(env.layer.children.length, 1)
  const offers = pc.offers

  session.control('local', { video: false })
  assert.equal(video.enabled, false)
  assert.equal(audio.enabled, true)
  assert.deepEqual(session.events('local').at(-1), { audio: true, video: false, screen: false })
  assert.equal(env.layer.children.length, 0, 'a disabled camera paints no self view')
  assert.deepEqual(JSON.parse(pc.channels[0].sent.at(-1)), {
    audio: true, video: false, screen: false, screenStream: null,
  })

  session.control('local', { audio: false, video: true })
  assert.deepEqual(session.events('local').at(-1), { audio: false, video: true, screen: false })
  assert.equal(env.layer.children.length, 1)
  audio.level = 0.5
  env.timers.fire()
  assert.equal(session.events('levels').at(-1).self, 0, 'a muted microphone reads silent')

  session.control('local', { audio: true })
  env.timers.fire()
  assert.ok(session.events('levels').at(-1).self > 0)

  video.end()
  assert.deepEqual(session.events('local').at(-1), { audio: true, video: false, screen: false })
  assert.equal(env.layer.children.length, 0)
  await settle(4)
  assert.equal(pc.offers, offers, 'muting never renegotiates')
})

test('audio-resume retries a suspended AudioContext and reports playback', async () => {
  const env = environment({ audio: { state: 'suspended', resumable: false } })
  const session = openCall(env, { audio: true })
  const ready = await session.ready
  assert.equal(ready.playback, 'suspended')
  const context = env.contexts[0]
  assert.ok(context.resumes >= 1, 'open tried to start playback')

  context.resumable = true
  session.control('audio-resume')
  await settle(2)
  assert.deepEqual(session.events('playback'), [{ state: 'running' }])
})

test('finish releases every resource, reports duration, and is idempotent', async () => {
  const env = environment()
  const session = openCall(env, { audio: true, video: true })
  await session.ready
  session.control('connect', { peer: 'p1', polite: true })
  session.control('connect', { peer: 'p2', polite: false })
  await settle(4)
  const [pc1, pc2] = env.rtc.instances
  pc1.receive('audio')
  pc1.receive('video')
  pc1.simulateConnected()
  await settle(2)
  session.control('tiles', {
    tiles: [
      { peer: 'p1', x: 0, y: 0, width: 50, height: 50 },
      { peer: 'self', x: 60, y: 0, width: 50, height: 50 },
    ],
  })
  assert.equal(env.layer.children.length, 2)
  const painted = env.layer.children.map((tile) => tile.children[0])
  const sink = env.document.created.find((element) => element.tagName === 'AUDIO')
  const logLength = () => session.log.length

  env.advance(4_250)
  session.control('finish')
  assert.deepEqual(await session.result, { durationMs: 4_250 })

  assert.ok(pc1.closed && pc2.closed)
  assert.ok(pc1.channels[0].readyState === 'closed')
  assert.deepEqual(env.devices.granted[0].getTracks().map((track) => track.stops), [1, 1])
  assert.equal(env.layer.children.length, 0)
  assert.ok(painted.every((video) => video.srcObject === null))
  assert.equal(sink.srcObject, null)
  assert.equal(env.contexts[0].state, 'closed')
  assert.ok(env.contexts[0].nodes.every((node) => node.disconnected))
  assert.equal(env.timers.active.length, 0)

  const settled = logLength()
  session.control('finish')
  session.control('cancel')
  session.control('connect', { peer: 'p3', polite: true })
  pc1.receive('audio')
  env.timers.fire()
  await settle(4)
  assert.equal(logLength(), settled)
  assert.equal(env.rtc.instances.length, 2)
  assert.deepEqual(env.devices.granted[0].getTracks().map((track) => track.stops), [1, 1])
  assertAppSawOnlyJson(session)
})

test('the host keeps one call per frame and tears it down on deactivation and detach', async () => {
  const env = environment()
  const providers = builtInCapabilityProviders({ call: env.deps })
  assert.equal(providers[MEDIA_CALL].version, 1)
  const sent = []
  const source = { id: 'frame' }
  const host = createCapabilityHost({
    providers: { [MEDIA_CALL]: providers[MEDIA_CALL] },
    getDeclaration: () => ({ version: 1, lifecycle: 'active_frame', limits: { max_peers: 4 } }),
    isActive: () => true,
    send(_target, message) { sent.push(message) },
  })
  const open = (requestId) => host.handle(source, {
    type: 'moebius:capability-open',
    requestId,
    capability: MEDIA_CALL,
    version: 1,
    input: { audio: true, video: true },
  })
  const control = (requestId, action, value) => host.handle(source, {
    type: 'moebius:capability-control',
    requestId,
    capability: MEDIA_CALL,
    action,
    value,
  })
  const messages = (requestId, type) => sent.filter((message) => (
    message.requestId === requestId && message.type === type
  ))

  open('call-1')
  control('call-1', 'connect', { peer: 'p1', polite: true })
  control('call-1', 'tiles', { tiles: [{ peer: 'self', x: 0, y: 0, width: 80, height: 60 }] })
  await settle()
  assert.deepEqual(messages('call-1', 'moebius:capability-ready')[0].value, {
    audio: true, video: true, playback: 'running',
  })
  assert.equal(env.rtc.instances.length, 1, 'controls sent before readiness still apply')
  assert.equal(env.layer.children.length, 1)

  open('call-2')
  assert.equal(messages('call-2', 'moebius:capability-error')[0].code, 'busy')

  host.deactivate()
  await settle(4)
  assert.equal(messages('call-1', 'moebius:capability-error')[0].code, 'aborted')
  assert.equal(env.rtc.instances[0].closed, true)
  assert.deepEqual(env.devices.granted[0].getTracks().map((track) => track.stops), [1, 1])
  assert.equal(env.layer.children.length, 0)
  assert.equal(env.contexts[0].state, 'closed')
  assert.equal(host.activeCount(), 0)

  open('call-3')
  await settle()
  assert.equal(messages('call-3', 'moebius:capability-ready').length, 1)
  host.detachSource(source)
  await settle(4)
  const detached = messages('call-3', 'moebius:capability-error')
  assert.equal(detached.length, 1)
  assert.equal(detached[0].code, 'aborted')
  assert.match(detached[0].message, /detached/)
  assert.equal(env.contexts[1].state, 'closed')
  assert.deepEqual(env.devices.granted[1].getTracks().map((track) => track.stops), [1, 1])

  for (const message of sent) {
    assertPlainJson(message.value ?? null, `${message.type}.value`)
  }
})

test('the tile layer reuses elements, follows list order, and clears on destroy', () => {
  const document = fakeDocument()
  let container = document.createElement('div')
  const layer = createCallTileLayer({ getContainer: () => container })
  const first = new FakeStream([new FakeTrack('video')])
  const second = new FakeStream([new FakeTrack('video')])
  const tile = (peer, stream, x, mirror = false) => ({
    peer, stream, x, y: 4, width: 32, height: 24, radius: 6, opacity: 1, mirror,
  })

  layer.paint([tile('a', first, 0), tile('a', second, 40, true)])
  assert.equal(container.children.length, 2, 'one peer may be painted twice')
  const [one, two] = container.children
  assert.equal(one.children[0].srcObject, first)
  assert.equal(two.children[0].style.transform, 'scaleX(-1)')
  assert.equal(two.style.zIndex, '2')
  assert.equal(one.children[0].attributes.playsinline, '')
  assert.equal(one.children[0].playing, true)

  layer.paint([tile('a', second, 10)])
  assert.deepEqual(container.children, [one])
  assert.equal(one.children[0].srcObject, second)
  assert.equal(one.style.left, '10px')
  assert.equal(two.children[0].srcObject, null)

  const previous = container
  container = document.createElement('div')
  layer.paint([tile('a', second, 10)])
  assert.equal(previous.children.length, 0, 'a replaced container is cleaned up')
  assert.equal(container.children.length, 1)

  layer.destroy()
  assert.equal(container.children.length, 0)
  container = null
  layer.paint([tile('a', first, 0)])
  layer.destroy()
})

test('the tile layer keeps a peer\'s camera and screen in separate elements', () => {
  const document = fakeDocument()
  const container = document.createElement('div')
  const layer = createCallTileLayer({ getContainer: () => container })
  const camera = new FakeStream([new FakeTrack('video')])
  const screen = new FakeStream([new FakeTrack('video')])
  const tile = (source, stream) => ({
    peer: 'a', source, stream, x: 0, y: 0, width: 32, height: 24, radius: 0, opacity: 1, mirror: false,
  })

  layer.paint([tile('camera', camera), tile('screen', screen)])
  const [cameraElement, screenElement] = container.children
  assert.equal(cameraElement.children[0].style.objectFit, 'cover')
  assert.equal(screenElement.children[0].style.objectFit, 'contain', 'a screen is letterboxed, never cropped')

  // The camera going dark leaves the screen's element and playback alone.
  layer.paint([tile('screen', screen)])
  assert.deepEqual(container.children, [screenElement])
  assert.equal(screenElement.children[0].srcObject, screen)
  layer.destroy()
})

test('screen sharing needs the reviewed screen_share limit and a capable browser', async () => {
  const env = environment()
  const session = openCall(env, { audio: true })
  await session.ready

  session.control('screen', { share: true })
  await settle()
  assert.deepEqual(session.events('error'), [{
    peer: null,
    code: 'denied',
    message: 'This app is not allowed to share your screen. Its installed access does not include screen sharing.',
  }])
  assert.deepEqual(env.devices.displayRequests, [], 'an undeclared app never reaches the picker')
  assert.deepEqual(session.events('local'), [])

  const old = environment()
  delete old.devices.getDisplayMedia
  const unsupported = openCall(old, { audio: true }, SHARING)
  await unsupported.ready
  unsupported.control('screen', { share: true })
  await settle()
  assert.deepEqual(
    unsupported.events('error').map(({ peer, code }) => [peer, code]),
    [[null, 'unavailable']],
  )

  // Neither ends the call.
  session.control('finish')
  unsupported.control('finish')
  assert.deepEqual(await session.result, { durationMs: 0 })
  assert.deepEqual(await unsupported.result, { durationMs: 0 })
  assertAppSawOnlyJson(session)
  assertAppSawOnlyJson(unsupported)
})

test('sharing a screen renegotiates, and the far side paints it beside the camera', async () => {
  const { aliceEnv, bobEnv, alice, bob, alicePc, bobPc } = await connectedPair()
  bob.control('tiles', ALICE_TILES)
  assert.equal(paintedTiles(bobEnv).length, 1, 'nothing is shared yet: only the camera paints')
  const offers = alicePc.offers + bobPc.offers

  alice.control('screen', { share: true })
  await settle(80)

  assert.deepEqual(aliceEnv.devices.displayRequests, [{
    video: { frameRate: { ideal: 15, max: 30 }, width: { max: 1920 }, height: { max: 1080 } },
    audio: false,
  }])
  const [screen] = aliceEnv.devices.displays[0].getVideoTracks()
  assert.equal(screen.contentHint, 'detail')
  assert.deepEqual(alice.events('local').at(-1), { audio: true, video: true, screen: true })
  const camera = alicePc.senders.find(({ track }) => track.kind === 'video' && track !== screen)
  const shared = alicePc.senders.find(({ track }) => track === screen)
  assert.ok(shared, 'the screen is sent to the connected peer')
  assert.notEqual(shared.streamId, camera.streamId, 'in its own stream')
  assert.ok(alicePc.offers + bobPc.offers > offers, 'adding the screen renegotiated')
  assert.deepEqual(JSON.parse(alicePc.channels[0].sent.at(-1)), {
    audio: true, video: true, screen: true, screenStream: shared.streamId,
  })
  assert.deepEqual(bob.events('peer').at(-1), {
    peer: 'alice', state: 'connected', audio: true, video: true, screen: true,
  })

  const tiles = paintedTiles(bobEnv)
  assert.equal(tiles.length, 2, 'the camera and the screen paint separately')
  assert.equal(tiles[0].track, bobPc.received.get('video'))
  assert.equal(tiles[0].video.style.objectFit, 'cover')
  assert.equal(tiles[1].track, bobPc.received.get('video#2'))
  assert.equal(tiles[1].video.style.objectFit, 'contain')
  assert.equal(tiles[1].video.style.transform, '')
  assert.equal(tiles[1].style.width, '320px')

  // Alice previews her own share, unmirrored, beside her mirrored camera.
  alice.control('tiles', {
    tiles: [
      { peer: 'self', x: 0, y: 0, width: 64, height: 48 },
      { peer: 'self', source: 'screen', x: 70, y: 0, width: 160, height: 90 },
    ],
  })
  const own = paintedTiles(aliceEnv)
  assert.equal(own.length, 2)
  assert.equal(own[0].track, camera.track)
  assert.equal(own[0].video.style.transform, 'scaleX(-1)')
  assert.equal(own[1].track, screen)
  assert.equal(own[1].video.style.transform, '', 'a self screen preview is not mirrored')

  // One screen per call: asking again changes nothing.
  alice.control('screen', { share: true })
  await settle(4)
  assert.equal(aliceEnv.devices.displayRequests.length, 1)
  assert.equal(alicePc.senders.length, 3)
  assert.deepEqual(alice.events('error'), [])
  assert.deepEqual(bob.events('error'), [])
  assertAppSawOnlyJson(alice)
  assertAppSawOnlyJson(bob)
})

test('someone who connects later receives the screen already being shared', async () => {
  const rtc = fakeRtc()
  const aliceEnv = environment({ rtc })
  const carolEnv = environment({ rtc })
  // Alice presents without a camera.
  const alice = openCall(aliceEnv, { audio: true }, SHARING)
  await alice.ready
  alice.control('screen', { share: true })
  await settle(4)
  assert.deepEqual(alice.events('local').at(-1), { audio: true, video: false, screen: true })

  const carol = openCall(carolEnv, { audio: true, video: true }, SHARING)
  await carol.ready
  relay(alice, 'alice', carol, 'carol')
  relay(carol, 'carol', alice, 'alice')
  carol.control('tiles', ALICE_TILES)
  carol.control('connect', { peer: 'alice', polite: false })
  await settle(120)

  const [carolPc, alicePc] = rtc.instances
  const [screen] = aliceEnv.devices.displays[0].getVideoTracks()
  const shared = alicePc.senders.find(({ track }) => track === screen)
  assert.ok(shared, 'a new connection also gets the screen')
  // Alice adds the screen only once her state channel is open, so Carol
  // learns which stream is the screen before its track arrives.
  const announced = carolPc.arrivals.findIndex(([kind, data]) => (
    kind === 'state' && JSON.parse(data).screenStream === shared.streamId
  ))
  const arrived = carolPc.arrivals.findIndex(([kind, key]) => kind === 'track' && key === 'video')
  assert.ok(announced >= 0 && announced < arrived, 'the announcement precedes the screen track')
  const fromAlice = carol.events('peer').filter(({ peer }) => peer === 'alice')
  assert.deepEqual(fromAlice.at(-1), {
    peer: 'alice', state: 'connected', audio: true, video: false, screen: true,
  })
  assert.ok(fromAlice.every(({ video }) => video === false), 'the screen never arrived looking like a camera')
  const tiles = paintedTiles(carolEnv)
  assert.equal(tiles.length, 1)
  assert.equal(tiles[0].track, carolPc.received.get('video'))
  assert.equal(tiles[0].video.style.objectFit, 'contain')
  assert.deepEqual(carol.events('error'), [])
  assertAppSawOnlyJson(alice)
  assertAppSawOnlyJson(carol)
})

test('stopping a share removes it everywhere, from the app or the browser\'s stop button', async () => {
  const { aliceEnv, bobEnv, alice, bob, alicePc, bobPc } = await connectedPair()
  bob.control('tiles', ALICE_TILES)
  alice.control('screen', { share: true })
  await settle(80)
  assert.equal(paintedTiles(bobEnv).length, 2)
  const [first] = aliceEnv.devices.displays[0].getVideoTracks()
  const firstStream = alicePc.senders.find(({ track }) => track === first).streamId

  let offers = alicePc.offers + bobPc.offers
  alice.control('screen', { share: false })
  assert.equal(first.stops, 1)
  assert.deepEqual(alice.events('local').at(-1), { audio: true, video: true, screen: false })
  assert.ok(!alicePc.senders.some(({ track }) => track === first), 'its sender is removed')
  await settle(80)
  assert.ok(alicePc.offers + bobPc.offers > offers, 'removing the screen renegotiated')
  assert.equal(bobPc.received.get('video#2').muted, true)
  assert.deepEqual(bob.events('peer').at(-1), {
    peer: 'alice', state: 'connected', audio: true, video: true, screen: false,
  })
  let tiles = paintedTiles(bobEnv)
  assert.equal(tiles.length, 1, 'the screen tile is gone')
  assert.equal(tiles[0].track, bobPc.received.get('video'), 'the camera still paints')

  // Stopping again is a no-op.
  const localEvents = alice.events('local').length
  alice.control('screen', { share: false })
  assert.equal(alice.events('local').length, localEvents)

  // Sharing again arrives in a new stream, which Bob paints as the screen.
  alice.control('screen', { share: true })
  await settle(80)
  const [second] = aliceEnv.devices.displays[1].getVideoTracks()
  const secondStream = alicePc.senders.find(({ track }) => track === second).streamId
  assert.notEqual(secondStream, firstStream)
  assert.deepEqual(bob.events('peer').at(-1), {
    peer: 'alice', state: 'connected', audio: true, video: true, screen: true,
  })
  tiles = paintedTiles(bobEnv)
  assert.equal(tiles.length, 2)
  assert.equal(tiles[0].track, bobPc.received.get('video'))
  assert.equal(tiles[1].track, bobPc.received.get('video#3'))

  // The browser's own "Stop sharing" control ends the track.
  offers = alicePc.offers + bobPc.offers
  second.end()
  assert.equal(second.stops, 1)
  assert.equal(second.onended, null)
  assert.deepEqual(alice.events('local').at(-1), { audio: true, video: true, screen: false })
  assert.ok(!alicePc.senders.some(({ track }) => track === second))
  await settle(80)
  assert.ok(alicePc.offers + bobPc.offers > offers)
  assert.deepEqual(bob.events('peer').at(-1), {
    peer: 'alice', state: 'connected', audio: true, video: true, screen: false,
  })
  tiles = paintedTiles(bobEnv)
  assert.equal(tiles.length, 1)
  assert.equal(tiles[0].track, bobPc.received.get('video'))
  assert.deepEqual(alice.events('error'), [])
  assert.deepEqual(bob.events('error'), [])
  assertAppSawOnlyJson(alice)
  assertAppSawOnlyJson(bob)
})

test('a remote screen is told from the camera by its announced stream, in either order', async () => {
  const env = environment()
  const session = openCall(env, { audio: true, video: true }, SHARING)
  await session.ready
  session.control('connect', { peer: 'p1', polite: false })
  session.control('connect', { peer: 'p2', polite: false })
  await settle(4)
  const [pc1, pc2] = env.rtc.instances
  const announce = (pc, { video = true, screenStream = null } = {}) => pc.channels[0].deliver(
    JSON.stringify({ audio: true, video, screen: screenStream !== null, screenStream }),
  )
  const camera = pc1.receive('video', 'p1-camera')
  pc1.simulateConnected()
  pc2.simulateConnected()
  await settle(2)
  announce(pc1)
  session.control('tiles', {
    tiles: [
      { peer: 'p1', x: 0, y: 0, width: 96, height: 72 },
      { peer: 'p1', source: 'screen', x: 0, y: 80, width: 320, height: 180 },
      { peer: 'p2', x: 100, y: 0, width: 96, height: 72 },
      { peer: 'p2', source: 'screen', x: 100, y: 80, width: 320, height: 180 },
    ],
  })
  const shown = () => paintedTiles(env).map(({ track, video }) => [track, video.style.objectFit])
  const latest = (peer) => session.events('peer').filter((value) => value.peer === peer).at(-1)
  assert.deepEqual(shown(), [[camera, 'cover']])

  // The announcement arrives before the track.
  announce(pc1, { screenStream: 'share-1' })
  assert.equal(latest('p1').screen, false, 'announced, but nothing has arrived yet')
  const first = pc1.receive('video#2', 'share-1')
  await settle(2)
  assert.deepEqual(latest('p1'), {
    peer: 'p1', state: 'connected', audio: false, video: true, screen: true,
  })
  assert.deepEqual(shown(), [[camera, 'cover'], [first, 'contain']])

  // Once stopped, the old screen never stands in for the camera.
  announce(pc1)
  assert.equal(latest('p1').screen, false)
  assert.deepEqual(shown(), [[camera, 'cover']])

  // A restarted share's track arrives before its announcement.
  const second = pc1.receive('video#3', 'share-2')
  await settle(2)
  assert.deepEqual(shown(), [[camera, 'cover']], 'unannounced video does not displace the camera')
  announce(pc1, { screenStream: 'share-2' })
  assert.equal(latest('p1').screen, true)
  assert.deepEqual(shown(), [[camera, 'cover'], [second, 'contain']])

  // A late track from the stopped share stays hidden.
  pc1.receive('video#4', 'share-1')
  await settle(2)
  assert.deepEqual(shown(), [[camera, 'cover'], [second, 'contain']])

  // A presenter without a camera: its unannounced screen is never a camera.
  announce(pc2, { video: false })
  const early = pc2.receive('video', 'p2-share')
  await settle(2)
  assert.equal(latest('p2').video, false)
  assert.deepEqual(shown(), [[camera, 'cover'], [second, 'contain']])
  announce(pc2, { video: false, screenStream: 'p2-share' })
  assert.deepEqual(latest('p2'), {
    peer: 'p2', state: 'connected', audio: false, video: false, screen: true,
  })
  assert.deepEqual(shown(), [[camera, 'cover'], [second, 'contain'], [early, 'contain']])

  // Malformed announcements cannot claim a screen.
  pc2.channels[0].deliver(JSON.stringify({
    audio: true, video: false, screen: true, screenStream: 'x'.repeat(65),
  }))
  assert.equal(latest('p2').screen, false)
  pc2.channels[0].deliver(JSON.stringify({ audio: true, video: false, screen: 'yes', screenStream: 'p2-share' }))
  assert.equal(latest('p2').screen, false)
  assert.deepEqual(shown(), [[camera, 'cover'], [second, 'contain']])

  // A screen that arrives before the camera it shares a call with never
  // fills the camera's tile.
  session.control('disconnect', { peer: 'p1' })
  session.control('disconnect', { peer: 'p2' })
  session.control('connect', { peer: 'p3', polite: false })
  await settle(4)
  const pc3 = env.rtc.instances[2]
  pc3.simulateConnected()
  await settle(2)
  session.control('tiles', {
    tiles: [
      { peer: 'p3', x: 0, y: 0, width: 96, height: 72 },
      { peer: 'p3', source: 'screen', x: 0, y: 80, width: 320, height: 180 },
    ],
  })
  announce(pc3, { screenStream: 'p3-share' })
  const slides = pc3.receive('video', 'p3-share')
  await settle(2)
  assert.deepEqual(latest('p3'), {
    peer: 'p3', state: 'connected', audio: false, video: false, screen: true,
  })
  assert.deepEqual(shown(), [[slides, 'contain']])
  const face = pc3.receive('video#2', 'p3-camera')
  await settle(2)
  assert.equal(latest('p3').video, true)
  assert.deepEqual(shown(), [[face, 'cover'], [slides, 'contain']])
  assertAppSawOnlyJson(session)
})

test('a cancelled or refused picker reports denied without ending the call', async () => {
  const env = environment({ devices: fakeDevices({ display: 'NotAllowedError' }) })
  const session = openCall(env, { audio: true, video: true }, SHARING)
  await session.ready
  session.control('connect', { peer: 'p1', polite: false })
  await settle(4)
  const pc = env.rtc.instances[0]
  pc.simulateConnected()
  await settle(2)

  session.control('screen', { share: true })
  await settle(4)
  env.devices.plan.display = 'InvalidStateError'
  session.control('screen', { share: true })
  await settle(4)
  env.devices.plan.display = 'NotFoundError'
  session.control('screen', { share: true })
  await settle(4)
  const errors = session.events('error')
  assert.deepEqual(
    errors.map(({ peer, code }) => [peer, code]),
    [[null, 'denied'], [null, 'denied'], [null, 'unavailable']],
  )
  assert.match(errors[0].message, /picker/)
  assert.match(errors[1].message, /click or key press/)
  assert.deepEqual(session.events('local'), [], 'nothing was shared')
  assert.equal(pc.senders.length, 2)
  assert.equal(pc.closed, false)

  // The person can simply try again.
  env.devices.plan.display = undefined
  session.control('screen', { share: true })
  await settle(4)
  assert.deepEqual(session.events('local').at(-1), { audio: true, video: true, screen: true })
  assert.equal(pc.senders.length, 3)
  assert.equal(session.log.some(([kind]) => kind === 'failure'), false)
  assertAppSawOnlyJson(session)
})

test('a picker that answers after the share was withdrawn or the call ended is stopped', async () => {
  const devices = fakeDevices({ deferDisplay: true })
  const env = environment({ devices })
  const session = openCall(env, { audio: true }, SHARING)
  await session.ready

  session.control('screen', { share: true })
  session.control('screen', { share: true })
  await settle(2)
  assert.equal(devices.pickers.length, 1, 'one picker at a time')
  session.control('screen', { share: false })
  const withdrawn = new FakeStream([new FakeTrack('video')])
  devices.pickers[0].resolve(withdrawn)
  await settle(4)
  assert.equal(withdrawn.getTracks()[0].stops, 1)
  assert.deepEqual(session.events('local'), [])

  // A picker that returns no live screen is released and reported.
  session.control('screen', { share: true })
  await settle(2)
  const ended = new FakeTrack('video')
  ended.readyState = 'ended'
  const unusable = new FakeStream([ended, new FakeTrack('audio')])
  devices.pickers[1].resolve(unusable)
  await settle(4)
  assert.deepEqual(unusable.getTracks().map((track) => track.stops), [1, 1])
  assert.deepEqual(
    session.events('error').map(({ peer, code }) => [peer, code]),
    [[null, 'unavailable']],
  )
  assert.deepEqual(session.events('local'), [])

  session.control('screen', { share: true })
  await settle(2)
  session.control('finish')
  await session.result
  const late = new FakeStream([new FakeTrack('video'), new FakeTrack('audio')])
  devices.pickers[2].resolve(late)
  await settle(4)
  assert.deepEqual(late.getTracks().map((track) => track.stops), [1, 1])

  const cancelled = openCall(environment({ devices }), { audio: true }, SHARING)
  await cancelled.ready
  cancelled.control('screen', { share: true })
  await settle(2)
  cancelled.control('cancel')
  devices.pickers[3].reject(domError('NotAllowedError'))
  await settle(4)
  assert.deepEqual(cancelled.events('error'), [], 'a call that already ended reports nothing more')
  assertAppSawOnlyJson(session)
})

test('ending or hiding the call stops a shared screen', async () => {
  const env = environment()
  const session = openCall(env, { audio: true }, SHARING)
  await session.ready
  session.control('connect', { peer: 'p1', polite: false })
  await settle(4)
  const pc = env.rtc.instances[0]
  session.control('screen', { share: true })
  await settle(4)
  const [screen] = env.devices.displays[0].getVideoTracks()
  assert.ok(!pc.senders.some(({ track }) => track === screen), 'sent only once the state channel opens')
  pc.simulateConnected()
  await settle(4)
  assert.ok(pc.senders.some(({ track }) => track === screen))
  session.control('tiles', {
    tiles: [{ peer: 'self', source: 'screen', x: 0, y: 0, width: 160, height: 90 }],
  })
  assert.equal(env.layer.children.length, 1)

  session.control('finish')
  await session.result
  assert.equal(screen.stops, 1)
  assert.equal(screen.onended, null)
  assert.equal(pc.closed, true)
  assert.equal(env.layer.children.length, 0)

  const hosted = environment()
  const providers = builtInCapabilityProviders({ call: hosted.deps })
  const sent = []
  const source = { id: 'frame' }
  const host = createCapabilityHost({
    providers: { [MEDIA_CALL]: providers[MEDIA_CALL] },
    getDeclaration: () => ({
      version: 1, lifecycle: 'active_frame', limits: { max_peers: 4, screen_share: 1 },
    }),
    isActive: () => true,
    send(_target, message) { sent.push(message) },
  })
  const message = (type, fields) => host.handle(source, {
    type, requestId: 'call', capability: MEDIA_CALL, ...fields,
  })
  message('moebius:capability-open', { version: 1, input: { audio: true } })
  // Sent before readiness, it waits for the microphone like any other control.
  message('moebius:capability-control', { action: 'screen', value: { share: true } })
  await settle()
  const [hostedScreen] = hosted.devices.displays[0].getVideoTracks()
  const local = sent.filter((entry) => entry.event === 'local')
  assert.deepEqual(local.at(-1).value, { audio: true, video: false, screen: true })

  host.deactivate()
  await settle(4)
  assert.equal(hostedScreen.stops, 1)
  assert.equal(sent.find((entry) => entry.type === 'moebius:capability-error').code, 'aborted')
  assert.equal(host.activeCount(), 0)
  for (const entry of sent) assertPlainJson(entry.value ?? null, `${entry.type}.value`)
})
