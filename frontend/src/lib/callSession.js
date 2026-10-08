/**
 * Shell side of `media.call` v1: live audio and video calls for opaque apps.
 *
 * The trusted shell owns every media object: the microphone and camera, a
 * shared screen, each RTCPeerConnection, Web Audio playback, and the painted
 * video tiles. The app frame relays this provider's opaque `signal` payloads
 * between participants over its own channels and steers per-peer volume and
 * tile rectangles. No MediaStream, track, connection, or DOM handle crosses the
 * capability channel: every ready, event, and result value is plain JSON.
 *
 * Peers use the standard "perfect negotiation" pattern, so either side may
 * start and offer glare resolves through the app-chosen `polite` flag. A
 * negotiated data channel (id 0) carries only each side's own
 * `{audio, video, screen, screenStream}` send state, so a remote camera turned
 * off hides its tile and reveals the app's placeholder instead of painting
 * black frames, and a remote video track is known to be a shared screen when
 * it arrives in the announced `screenStream`. Every app-supplied value is
 * checked by callRequest.js first.
 */

import {
  assertFields, callError, clamp, finiteNumber, invalid, isPlainObject, readCallRequest,
  readPeer, readScreenControl, readSignalData, readTiles, requirePeer, SELF,
} from './callRequest.js'

export const MEDIA_CALL = 'media.call'

const MAX_GAINS = 64
const MAX_PENDING_CONTROLS = 256
const MAX_STATE_MESSAGE_CHARS = 256
// An SDP msid stream id is at most 64 characters (RFC 8830).
const MAX_STREAM_ID_CHARS = 64
// Stream ids of a peer's earlier shares, remembered so a stopped screen never
// returns looking like a camera.
const MAX_SCREEN_IDS = 16
const LEVEL_INTERVAL_MS = 200
const GAIN_TIME_CONSTANT_S = 0.05
const STATE_CHANNEL_ID = 0

const CONTROLS = new Set(['connect', 'signal', 'disconnect', 'volume', 'tiles', 'local', 'screen'])
const PEER_STATES = {
  new: 'connecting',
  checking: 'connecting',
  connecting: 'connecting',
  connected: 'connected',
  completed: 'connected',
  disconnected: 'disconnected',
  failed: 'failed',
  closed: 'closed',
}

function descriptionSignal(description) {
  return {
    description: {
      type: String(description?.type || ''),
      sdp: String(description?.sdp || ''),
    },
  }
}

function candidateSignal(candidate) {
  if (!candidate) return { candidate: null }
  const json = typeof candidate.toJSON === 'function' ? candidate.toJSON() : candidate
  const value = { candidate: typeof json?.candidate === 'string' ? json.candidate : '' }
  if (typeof json?.sdpMid === 'string') value.sdpMid = json.sdpMid
  if (Number.isInteger(json?.sdpMLineIndex)) value.sdpMLineIndex = json.sdpMLineIndex
  if (typeof json?.usernameFragment === 'string') value.usernameFragment = json.usernameFragment
  return { candidate: value }
}

function deniedMedia(error) {
  return ['NotAllowedError', 'PermissionDeniedError', 'SecurityError'].includes(error?.name)
}

function mediaErrorCode(error) {
  return deniedMedia(error) ? 'denied' : 'unavailable'
}

function mediaFailure(errors) {
  const denied = errors.find(deniedMedia)
  const cause = denied || errors[errors.length - 1]
  return callError(
    denied ? 'denied' : 'unavailable',
    denied
      ? 'Microphone or camera access was denied. Allow it for this site in the browser, then try again.'
      : 'No usable microphone or camera was found, or another app is using it.',
    typeof cause?.name === 'string' && cause.name
      ? cause.name
      : (denied ? 'NotAllowedError' : 'NotFoundError'),
  )
}

function mediaConstraints(audio, video) {
  return {
    audio: audio
      ? { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      : false,
    video: video
      ? {
          facingMode: 'user',
          width: { ideal: 640 },
          height: { ideal: 360 },
          frameRate: { ideal: 24 },
        }
      : false,
  }
}

// Screens favour legible detail over motion: a modest frame rate, capped at
// 1080p so a large display does not flood every connection of the mesh.
function displayConstraints() {
  return {
    video: { frameRate: { ideal: 15, max: 30 }, width: { max: 1920 }, height: { max: 1080 } },
    audio: false,
  }
}

function screenFailure(error) {
  if (error?.name === 'InvalidStateError') {
    // The browser opens its picker only for a recent click or key press.
    return callError(
      'denied',
      'Screen sharing must start from a click or key press in the app. Try again.',
      'NotAllowedError',
    )
  }
  if (deniedMedia(error)) {
    return callError(
      'denied',
      'Screen sharing was cancelled or blocked. Try again and choose what to share in the browser\'s picker.',
      'NotAllowedError',
    )
  }
  return callError('unavailable', 'No screen could be shared from this device.', 'NotFoundError')
}

function liveEnabled(track) {
  return Boolean(track) && track.readyState !== 'ended' && track.enabled !== false
}

function receiving(track) {
  return Boolean(track) && track.readyState !== 'ended' && !track.muted
}

function releaseTrackHandlers(track) {
  if (!track) return
  track.onmute = null
  track.onunmute = null
  track.onended = null
}

function stopStream(stream) {
  for (const track of stream?.getTracks?.() || []) {
    track.onended = null
    try { track.stop() } catch { /* already stopped */ }
  }
}

function disconnectNodes(...nodes) {
  for (const node of nodes) {
    try { node?.disconnect?.() } catch { /* already disconnected */ }
  }
}

function createMeter(audioContext, stream) {
  const source = audioContext.createMediaStreamSource(stream)
  const analyser = audioContext.createAnalyser()
  analyser.fftSize = 512
  source.connect(analyser)
  return { source, analyser, samples: null, smoothed: 0 }
}

// A smoothed 0..1 speaking level: -60 dBFS reads as silence, -10 dBFS as full.
// It rises quickly and decays slowly so meters do not flicker between words.
function measure(meter) {
  const { analyser } = meter
  const size = analyser.fftSize || 512
  let sum = 0
  if (typeof analyser.getFloatTimeDomainData === 'function') {
    if (!(meter.samples instanceof Float32Array) || meter.samples.length !== size) {
      meter.samples = new Float32Array(size)
    }
    analyser.getFloatTimeDomainData(meter.samples)
    for (const sample of meter.samples) sum += sample * sample
  } else if (typeof analyser.getByteTimeDomainData === 'function') {
    if (!(meter.samples instanceof Uint8Array) || meter.samples.length !== size) {
      meter.samples = new Uint8Array(size)
    }
    analyser.getByteTimeDomainData(meter.samples)
    for (const byte of meter.samples) sum += ((byte - 128) / 128) ** 2
  }
  const rms = Math.sqrt(sum / size)
  const raw = rms > 0 ? clamp((20 * Math.log10(rms) + 60) / 50, 0, 1) : 0
  const previous = meter.smoothed
  meter.smoothed = raw >= previous
    ? raw * 0.7 + previous * 0.3
    : raw * 0.35 + previous * 0.65
  return Math.round(meter.smoothed * 100) / 100
}

function startCall(request, channel, environment) {
  const {
    mediaDevices, RTCPeerConnectionCtor, MediaStreamCtor, AudioContextCtor,
    createElement, createSurface, now, startInterval, stopInterval,
  } = environment
  if (
    typeof RTCPeerConnectionCtor !== 'function'
    || typeof MediaStreamCtor !== 'function'
    || typeof AudioContextCtor !== 'function'
  ) {
    throw callError('unavailable', 'Live calls are unavailable in this browser.', 'NotSupportedError')
  }
  if ((request.audio || request.video) && typeof mediaDevices?.getUserMedia !== 'function') {
    throw callError(
      'unavailable',
      'Microphone and camera access is unavailable in this browser.',
      'NotSupportedError',
    )
  }
  let audioContext
  try {
    audioContext = new AudioContextCtor()
  } catch {
    throw callError('unavailable', 'Call audio is unavailable in this browser.', 'NotSupportedError')
  }

  const peers = new Map()
  const gains = new Map()
  const pending = []
  let phase = 'starting'
  let localStream = null
  let localAudio = null
  let localVideo = null
  let selfVideo = null
  let selfMeter = null
  // A shared screen travels in its own MediaStream, so the far side can tell
  // it from the camera by the stream id this side announces.
  let screenTrack = null
  let screenStream = null
  let screenRequest = null
  // A microphone or camera asked for after the call joined without it.
  let deviceRequest = null
  let levelTimer = null
  let startedAt = null
  let tiles = []
  let surface = null
  let playback = playbackState()

  function playbackState() {
    return audioContext.state === 'running' ? 'running' : 'suspended'
  }

  function notePlayback() {
    if (phase === 'ended') return
    const next = playbackState()
    if (next === playback) return
    playback = next
    if (phase === 'live') channel.event('playback', { state: next })
  }

  function resumeAudio() {
    if (phase === 'ended' || audioContext.state === 'running' || audioContext.state === 'closed') {
      return
    }
    try {
      Promise.resolve(audioContext.resume?.()).then(notePlayback, () => {})
    } catch { /* a later gesture can retry through audio-resume */ }
  }

  function current(peer) {
    return phase === 'live' && peers.get(peer.id) === peer
  }

  function report(peer, error) {
    if (phase === 'ended') return
    channel.event('error', {
      peer: peer || null,
      code: typeof error?.code === 'string' ? error.code : 'provider_error',
      message: typeof error?.message === 'string' && error.message
        ? error.message
        : 'The call hit an unexpected problem.',
    })
  }

  async function acquireMedia() {
    if (!request.audio && !request.video) return { stream: null }
    const failures = []
    const attempt = async (audio, video) => {
      try {
        return await mediaDevices.getUserMedia(mediaConstraints(audio, video))
      } catch (error) {
        failures.push(error)
        return null
      }
    }
    const stream = await attempt(request.audio, request.video)
    if (stream) return { stream }
    if (!request.audio || !request.video || phase !== 'starting') throw mediaFailure(failures)
    // Keep whichever half still works. A refused microphone is not asked
    // about again; a missing or busy one still lets the camera join.
    const audioOnly = await attempt(true, false)
    if (audioOnly) return { stream: audioOnly, videoError: mediaErrorCode(failures[0]) }
    if (deniedMedia(failures[1]) || phase !== 'starting') throw mediaFailure(failures)
    const videoOnly = await attempt(false, true)
    if (videoOnly) return { stream: videoOnly, audioError: mediaErrorCode(failures[1]) }
    throw mediaFailure(failures)
  }

  function begin({ stream, videoError, audioError }) {
    if (phase !== 'starting') {
      // Cancelled while the permission prompt was open: release late tracks.
      stopStream(stream)
      return
    }
    localStream = stream
    localAudio = stream?.getAudioTracks?.()[0] || null
    localVideo = stream?.getVideoTracks?.()[0] || null
    for (const track of [localAudio, localVideo]) {
      if (track) track.onended = localChanged
    }
    if (localVideo) selfVideo = new MediaStreamCtor([localVideo])
    if (localAudio) {
      try {
        selfMeter = createMeter(audioContext, new MediaStreamCtor([localAudio]))
      } catch {
        selfMeter = null // The call still works; only the self level stays 0.
      }
    }
    phase = 'live'
    startedAt = now()
    levelTimer = startInterval(emitLevels, LEVEL_INTERVAL_MS)
    playback = playbackState()
    const ready = {
      audio: liveEnabled(localAudio),
      video: liveEnabled(localVideo),
      playback,
    }
    if (videoError) ready.videoError = videoError
    if (audioError) ready.audioError = audioError
    channel.ready(ready)
    for (const [action, value] of pending.splice(0)) {
      if (phase !== 'live') break
      apply(action, value)
    }
    // Browsers that gate playback on activation also allow it once capture is live.
    resumeAudio()
  }

  function abortStart(error) {
    if (phase === 'ended') return
    teardown()
    channel.error(error)
  }

  function emitLevels() {
    if (phase !== 'live') return
    const remote = {}
    let measured = false
    for (const peer of peers.values()) {
      if (!peer.audio) continue
      remote[peer.id] = measure(peer.audio)
      measured = true
    }
    let self = 0
    if (selfMeter) {
      measured = true
      const level = measure(selfMeter)
      self = liveEnabled(localAudio) ? level : 0
    }
    if (measured) channel.event('levels', { self, peers: remote })
  }

  // A remote video track is the peer's shared screen when it arrived in the
  // stream that peer announced for sharing; any other is its camera. Tracks of
  // earlier shares are dropped, so a stopped screen never becomes the camera.
  function cameraOf(peer) {
    for (const entry of peer.videos.values()) {
      if (!peer.screenIds.has(entry.streamId)) return entry
    }
    return null
  }

  function screenOf(peer) {
    const id = peer.remote.screenStream
    if (!id) return null
    for (const entry of peer.videos.values()) {
      if (entry.streamId === id) return entry
    }
    return null
  }

  function snapshot(peer) {
    const { pc } = peer
    const state = PEER_STATES[pc.connectionState ?? pc.iceConnectionState] || 'connecting'
    const flowing = state === 'connected'
    return {
      peer: peer.id,
      state,
      audio: flowing && peer.remote.audio && receiving(peer.audio?.track),
      video: flowing && peer.remote.video && receiving(cameraOf(peer)?.track),
      screen: flowing && peer.remote.screen && receiving(screenOf(peer)?.track),
    }
  }

  function emitPeer(peer) {
    const value = snapshot(peer)
    const key = `${value.state}|${value.audio}|${value.video}|${value.screen}`
    if (peer.reported === key) return false
    peer.reported = key
    channel.event('peer', value)
    return true
  }

  function refreshPeer(peer) {
    if (current(peer) && emitPeer(peer)) repaint()
  }

  function screenLive() {
    return Boolean(screenTrack) && screenTrack.readyState !== 'ended'
  }

  function selfStream(source) {
    if (source === 'screen') return screenLive() ? screenStream : null
    return liveEnabled(localVideo) ? selfVideo : null
  }

  function peerStream(peer, source) {
    if (!peer) return null
    const value = snapshot(peer)
    if (source === 'screen') return value.screen ? screenOf(peer).stream : null
    return value.video ? cameraOf(peer).stream : null
  }

  function repaint() {
    if (phase !== 'live') return
    const painted = []
    for (const tile of tiles) {
      const stream = tile.peer === SELF
        ? selfStream(tile.source)
        : peerStream(peers.get(tile.peer), tile.source)
      // A tile without live video paints nothing; the app's own placeholder
      // underneath stays visible.
      if (stream) painted.push({ ...tile, stream })
    }
    if (!surface) {
      if (!painted.length || typeof createSurface !== 'function') return
      try { surface = createSurface() || null } catch { surface = null }
      if (!surface) return
    }
    try { surface.paint(painted) } catch { /* the frame is being torn down */ }
  }

  function localState() {
    return {
      audio: liveEnabled(localAudio),
      video: liveEnabled(localVideo),
      screen: screenLive(),
    }
  }

  function announceTo(peer) {
    if (!current(peer) || peer.channel?.readyState !== 'open') return
    const state = localState()
    // The far side needs the screen's stream id to tell it from the camera;
    // the app only ever sees the booleans.
    state.screenStream = state.screen && typeof screenStream.id === 'string'
      ? screenStream.id
      : null
    try { peer.channel.send(JSON.stringify(state)) } catch { /* closing */ }
  }

  function localChanged() {
    if (phase !== 'live') return
    for (const peer of peers.values()) announceTo(peer)
    channel.event('local', localState())
    repaint()
  }

  function releaseVideo(peer, track) {
    releaseTrackHandlers(track)
    peer.videos.delete(track)
  }

  function receiveState(peer, data) {
    if (!current(peer) || typeof data !== 'string' || data.length > MAX_STATE_MESSAGE_CHARS) return
    let state
    try { state = JSON.parse(data) } catch { return }
    if (!isPlainObject(state) || typeof state.audio !== 'boolean' || typeof state.video !== 'boolean') {
      return
    }
    const screenId = state.screen === true
      && typeof state.screenStream === 'string'
      && state.screenStream.length >= 1
      && state.screenStream.length <= MAX_STREAM_ID_CHARS
      ? state.screenStream
      : null
    peer.remote = {
      audio: state.audio,
      video: state.video,
      screen: screenId !== null,
      screenStream: screenId,
    }
    if (screenId) {
      peer.screenIds.delete(screenId)
      peer.screenIds.add(screenId)
      for (const old of peer.screenIds) {
        if (peer.screenIds.size <= MAX_SCREEN_IDS) break
        peer.screenIds.delete(old)
      }
    }
    // A stopped or replaced share's track is never shown again.
    for (const entry of [...peer.videos.values()]) {
      if (entry.streamId !== screenId && peer.screenIds.has(entry.streamId)) {
        releaseVideo(peer, entry.track)
      }
    }
    emitPeer(peer)
    // A restarted share keeps the same booleans but paints a new stream.
    repaint()
  }

  function setGain(node, value, { immediate = false } = {}) {
    const param = node.gain
    if (!immediate && typeof param.setTargetAtTime === 'function') {
      try {
        param.setTargetAtTime(value, audioContext.currentTime || 0, GAIN_TIME_CONSTANT_S)
        return
      } catch { /* fall through to an immediate value */ }
    }
    param.value = value
  }

  function releaseRemoteAudio(peer) {
    const audio = peer.audio
    if (!audio) return
    peer.audio = null
    releaseTrackHandlers(audio.track)
    disconnectNodes(audio.source, audio.analyser, audio.gain)
    if (audio.sink) {
      try { audio.sink.pause?.() } catch { /* already paused */ }
      audio.sink.srcObject = null
    }
  }

  function attachRemoteAudio(peer, track) {
    releaseRemoteAudio(peer)
    const stream = new MediaStreamCtor([track])
    // Chrome feeds a remote WebRTC stream into Web Audio only while a media
    // element also consumes it. This element stays muted; sound comes only
    // from the gain node below.
    const sink = createElement('audio')
    if (sink) {
      sink.muted = true
      sink.srcObject = stream
      try { sink.play?.()?.catch?.(() => {}) } catch { /* muted autoplay */ }
    }
    try {
      const meter = createMeter(audioContext, stream)
      const gain = audioContext.createGain()
      setGain(gain, gains.get(peer.id) ?? 1, { immediate: true })
      meter.source.connect(gain)
      gain.connect(audioContext.destination)
      peer.audio = { track, sink, gain, ...meter }
    } catch {
      if (sink) sink.srcObject = null
      report(peer.id, callError('provider_error', 'This person\'s audio could not be played.'))
      return
    }
    resumeAudio()
  }

  function receiveTrack(peer, track, streamId) {
    if (!current(peer) || !track) return
    if (track.kind === 'audio') {
      if (peer.audio?.track !== track) attachRemoteAudio(peer, track)
    } else if (track.kind === 'video') {
      const id = typeof streamId === 'string' ? streamId : null
      // A late track from a share the peer has already stopped stays hidden.
      if (peer.screenIds.has(id) && id !== peer.remote.screenStream) {
        releaseVideo(peer, track)
        return
      }
      const known = peer.videos.get(track)
      if (known) known.streamId = id
      else peer.videos.set(track, { track, streamId: id, stream: new MediaStreamCtor([track]) })
    } else {
      return
    }
    const refresh = () => refreshPeer(peer)
    track.onmute = refresh
    track.onunmute = refresh
    track.onended = refresh
    emitPeer(peer)
    repaint()
  }

  async function negotiate(peer) {
    if (!current(peer)) return
    try {
      peer.makingOffer = true
      await peer.pc.setLocalDescription()
      if (current(peer) && peer.pc.localDescription) {
        channel.event('signal', { peer: peer.id, data: descriptionSignal(peer.pc.localDescription) })
      }
    } catch {
      if (current(peer)) {
        report(peer.id, callError('provider_error', 'The call connection could not be negotiated.'))
      }
    } finally {
      peer.makingOffer = false
    }
  }

  function createPeer(id, polite) {
    if (peers.size >= request.maxPeers) {
      throw callError(
        'limit_exceeded',
        `This app can connect to at most ${request.maxPeers} people at once.`,
        'RangeError',
      )
    }
    let pc
    try {
      pc = new RTCPeerConnectionCtor({
        iceServers: request.iceServers.map((server) => ({ ...server, urls: [...server.urls] })),
      })
    } catch {
      throw callError('provider_error', 'The browser could not create a call connection.')
    }
    const peer = {
      id,
      pc,
      polite,
      makingOffer: false,
      ignoreOffer: false,
      settingRemoteAnswer: false,
      // Until the remote announces otherwise, trust live, unmuted tracks.
      remote: { audio: true, video: true, screen: false, screenStream: null },
      audio: null,
      // Remote video track -> { track, streamId, stream }, in arrival order.
      videos: new Map(),
      // Stream ids this peer has announced for sharing, oldest first.
      screenIds: new Set(),
      screenSender: null,
      channel: null,
      reported: '',
    }
    peers.set(id, peer)
    pc.onnegotiationneeded = () => { void negotiate(peer) }
    pc.onicecandidate = (event) => {
      if (current(peer)) {
        channel.event('signal', { peer: id, data: candidateSignal(event?.candidate) })
      }
    }
    pc.ontrack = (event) => receiveTrack(peer, event?.track, event?.streams?.[0]?.id)
    pc.onconnectionstatechange = () => refreshPeer(peer)
    pc.oniceconnectionstatechange = () => {
      if (!current(peer)) return
      if (pc.iceConnectionState === 'failed') {
        try { pc.restartIce?.() } catch { /* the app can disconnect and retry */ }
      }
      refreshPeer(peer)
    }
    try {
      const stateChannel = pc.createDataChannel('mobius-call-state', {
        negotiated: true,
        id: STATE_CHANNEL_ID,
      })
      stateChannel.onopen = () => {
        announceTo(peer)
        attachScreen(peer)
      }
      stateChannel.onmessage = (event) => receiveState(peer, event?.data)
      peer.channel = stateChannel
    } catch {
      // Without the state channel, remote media falls back to track liveness
      // and no screen is sent, since the far side could not identify it.
    }
    // A side without a local kind adds no transceiver for it: JSEP never reuses
    // an addTransceiver() receiver for a remote offer, so the sending side's
    // own negotiation adds that m-line and its media still arrives here.
    for (const track of [localAudio, localVideo]) {
      if (track && track.readyState !== 'ended') {
        try { pc.addTrack(track, localStream) } catch { /* sent nothing for this kind */ }
      }
    }
    emitPeer(peer)
    return peer
  }

  function closePeer(peer) {
    const { pc } = peer
    pc.onnegotiationneeded = null
    pc.onicecandidate = null
    pc.ontrack = null
    pc.onconnectionstatechange = null
    pc.oniceconnectionstatechange = null
    if (peer.channel) {
      peer.channel.onopen = null
      peer.channel.onmessage = null
      try { peer.channel.close() } catch { /* already closed */ }
    }
    releaseRemoteAudio(peer)
    for (const track of [...peer.videos.keys()]) releaseVideo(peer, track)
    peer.screenSender = null
    try { pc.close() } catch { /* already closed */ }
  }

  async function receiveDescription(peer, description) {
    const { pc } = peer
    const readyForOffer = !peer.makingOffer
      && (pc.signalingState === 'stable' || peer.settingRemoteAnswer)
    const offerCollision = description.type === 'offer' && !readyForOffer
    peer.ignoreOffer = !peer.polite && offerCollision
    if (peer.ignoreOffer) return
    peer.settingRemoteAnswer = description.type === 'answer'
    try {
      // A polite peer's own colliding offer rolls back implicitly here.
      await pc.setRemoteDescription(description)
    } finally {
      peer.settingRemoteAnswer = false
    }
    if (description.type !== 'offer' || !current(peer)) return
    await pc.setLocalDescription()
    if (current(peer) && pc.localDescription) {
      channel.event('signal', { peer: peer.id, data: descriptionSignal(pc.localDescription) })
    }
  }

  async function receiveCandidate(peer, candidate) {
    // End-of-candidates is optional; ICE completes without it.
    if (candidate === null) return
    try {
      await peer.pc.addIceCandidate(candidate)
    } catch (error) {
      // Candidates for an offer this impolite side ignored are expected to fail.
      if (!peer.ignoreOffer) throw error
    }
  }

  function connect(value) {
    assertFields(value, ['peer', 'polite'], 'call connect control')
    const id = requirePeer(value.peer)
    if (typeof value.polite !== 'boolean') {
      throw invalid('Call `connect` needs a boolean `polite` flag.')
    }
    const existing = peers.get(id)
    // Idempotent; an offer may already have created this peer as polite.
    if (existing) existing.polite = value.polite
    else createPeer(id, value.polite)
  }

  function signal(value) {
    assertFields(value, ['peer', 'data'], 'call signal control')
    const id = requirePeer(value.peer)
    const data = readSignalData(value.data)
    let peer = peers.get(id)
    if (!peer) {
      // Only an offer opens a connection. A late candidate or answer for a
      // peer this side already disconnected is stale, not a new call.
      if (data.description?.type !== 'offer') return
      peer = createPeer(id, true)
    }
    const work = data.description
      ? receiveDescription(peer, data.description)
      : receiveCandidate(peer, data.candidate)
    work.catch(() => {
      if (!current(peer)) return
      report(id, invalid(data.description
        ? 'The browser rejected the remote call description.'
        : 'The browser rejected a remote call network candidate.'))
    })
  }

  function disconnect(value) {
    assertFields(value, ['peer'], 'call disconnect control')
    const id = requirePeer(value.peer)
    gains.delete(id)
    const peer = peers.get(id)
    if (!peer) return
    peers.delete(id)
    closePeer(peer)
    channel.event('peer', { peer: id, state: 'closed', audio: false, video: false, screen: false })
    repaint()
  }

  function setVolumes(value) {
    assertFields(value, ['gains'], 'call volume control')
    if (!isPlainObject(value.gains)) throw invalid('Call `gains` must map peer ids to numbers.')
    const entries = Object.entries(value.gains)
    if (entries.length > MAX_GAINS) {
      throw callError('limit_exceeded', `At most ${MAX_GAINS} call volumes can be set at once.`, 'RangeError')
    }
    for (const [key, requested] of entries) {
      const id = readPeer(key)
      if (!id) {
        report(null, invalid('Call volume keys must be call peer ids.'))
        continue
      }
      if (!finiteNumber(requested)) {
        report(id, invalid('A call volume must be a finite number from 0 to 1.'))
        continue
      }
      if (!gains.has(id) && !peers.has(id) && gains.size >= MAX_GAINS) {
        report(id, callError('limit_exceeded', `At most ${MAX_GAINS} call volumes are remembered.`, 'RangeError'))
        continue
      }
      // Remembered before a peer connects, so its audio starts at this level.
      const gain = clamp(requested, 0, 1)
      gains.set(id, gain)
      const node = peers.get(id)?.audio?.gain
      if (node) setGain(node, gain)
    }
  }

  function setTiles(value) {
    // Room for every participant's camera and shared screen, self included.
    tiles = readTiles(value, 2 * (request.maxPeers + 1))
    repaint()
  }

  function setLocal(value) {
    assertFields(value, ['audio', 'video'], 'call local control')
    for (const kind of ['audio', 'video']) {
      if (value[kind] !== undefined && typeof value[kind] !== 'boolean') {
        throw invalid(`Call local \`${kind}\` must be true or false.`)
      }
    }
    // Muting toggles the track, so no renegotiation and instant unmute.
    if (typeof value.audio === 'boolean' && localAudio) localAudio.enabled = value.audio
    if (typeof value.video === 'boolean' && localVideo) localVideo.enabled = value.video
    // A kind the call joined without is asked for the first time the app
    // turns it on, so a receive-only listener can still speak up later.
    // While the browser asks, the latest on or off for a kind being asked
    // for decides whether it is kept when it arrives.
    for (const kind of ['audio', 'video']) {
      if (deviceRequest?.asked[kind] && typeof value[kind] === 'boolean') deviceRequest[kind] = value[kind]
    }
    const audio = value.audio === true && !localAudio
    const video = value.video === true && !localVideo
    if (audio || video) requestDevices(audio, video)
    localChanged()
  }

  function requestDevices(audio, video) {
    // One request at a time: turning a kind on again while asking is a no-op.
    if (deviceRequest) return
    if (typeof mediaDevices?.getUserMedia !== 'function') {
      report(null, callError(
        'unavailable',
        'Microphone and camera access is unavailable in this browser.',
        'NotSupportedError',
      ))
      return
    }
    const attempt = { asked: { audio, video }, audio, video }
    deviceRequest = attempt
    let asked
    try {
      asked = mediaDevices.getUserMedia(mediaConstraints(audio, video))
    } catch (error) {
      asked = Promise.reject(error)
    }
    Promise.resolve(asked).then((stream) => {
      if (deviceRequest !== attempt || phase !== 'live') {
        // The call ended while the permission prompt was open.
        stopStream(stream)
        return
      }
      deviceRequest = null
      addDevices(stream, attempt)
    }, (error) => {
      if (deviceRequest !== attempt) return
      deviceRequest = null
      // A refused or missing device leaves the call as it was.
      report(null, mediaFailure([error]))
    })
  }

  function addDevices(stream, wanted) {
    let added = false
    let withdrawn = false
    for (const track of stream?.getTracks?.() || []) {
      const missing = track.readyState !== 'ended'
        && ((track.kind === 'audio' && !localAudio) || (track.kind === 'video' && !localVideo))
      const keep = missing && wanted[track.kind] === true
      if (missing && !keep) withdrawn = true
      if (!keep) {
        // Never keep a capture the call does not use, or one turned off again.
        try { track.stop() } catch { /* already stopped */ }
        continue
      }
      track.onended = localChanged
      if (track.kind === 'audio') {
        localAudio = track
        try {
          selfMeter = createMeter(audioContext, new MediaStreamCtor([track]))
        } catch {
          selfMeter = null // The call still works; only the self level stays 0.
        }
      } else {
        localVideo = track
        selfVideo = new MediaStreamCtor([track])
      }
      added = true
    }
    if (!added) {
      // Everything granted was turned off again meanwhile: nothing to report.
      if (!withdrawn) report(null, mediaFailure([]))
      return
    }
    // One local stream holds every device track, so connections made later
    // send them together and ending the call stops them all.
    localStream = new MediaStreamCtor([localAudio, localVideo].filter(Boolean))
    for (const peer of peers.values()) {
      if (!current(peer)) continue
      for (const track of stream.getTracks()) {
        if (track !== localAudio && track !== localVideo) continue
        // Adding a track renegotiates through perfect negotiation.
        try { peer.pc.addTrack(track, localStream) } catch { /* this connection keeps receiving */ }
      }
    }
    localChanged()
  }

  // The screen joins a connection only once its state channel is open, so the
  // announcement naming the screen's stream always leaves before the
  // renegotiation that delivers the track, and even a late joiner never sees a
  // screen arrive looking like a camera.
  function attachScreen(peer) {
    if (!current(peer) || !screenLive() || peer.screenSender) return
    if (peer.channel?.readyState !== 'open') return
    try {
      peer.screenSender = peer.pc.addTrack(screenTrack, screenStream)
    } catch {
      // This connection simply receives no screen.
    }
  }

  function detachScreen(peer) {
    const sender = peer.screenSender
    if (!sender) return
    peer.screenSender = null
    // Removing the sender renegotiates, and the far side's track goes quiet.
    try { peer.pc.removeTrack(sender) } catch { /* the connection is closing */ }
  }

  function beginScreen(stream) {
    const track = stream?.getVideoTracks?.()[0] || null
    let wrapped = null
    if (track && track.readyState !== 'ended') {
      try { wrapped = new MediaStreamCtor([track]) } catch { /* reported below */ }
    }
    if (!wrapped) {
      // Never leave a capture running that the call does not own.
      stopStream(stream)
      report(null, screenFailure(null))
      return
    }
    // Only the screen itself is kept from what the picker returned.
    for (const other of stream.getTracks?.() || []) {
      if (other === track) continue
      try { other.stop() } catch { /* already stopped */ }
    }
    // Keep text and edges legible when bandwidth is short.
    if ('contentHint' in track) track.contentHint = 'detail'
    screenTrack = track
    screenStream = wrapped
    // The browser's own "Stop sharing" control ends the track.
    track.onended = stopScreen
    localChanged()
    for (const peer of peers.values()) attachScreen(peer)
  }

  function stopScreen() {
    // A picker that is still open settles into a stopped track.
    screenRequest = null
    if (!screenTrack) return
    const track = screenTrack
    screenTrack = null
    screenStream = null
    track.onended = null
    try { track.stop() } catch { /* already stopped */ }
    for (const peer of peers.values()) detachScreen(peer)
    localChanged()
  }

  function shareScreen(value) {
    if (!readScreenControl(value)) {
      stopScreen()
      return
    }
    if (!request.screenShare) {
      throw callError(
        'denied',
        'This app is not allowed to share your screen. Its installed access does not include screen sharing.',
        'NotAllowedError',
      )
    }
    // One screen per call: asking again while sharing or choosing is a no-op.
    if (screenTrack || screenRequest) return
    if (typeof mediaDevices?.getDisplayMedia !== 'function') {
      throw callError('unavailable', 'Screen sharing is unavailable in this browser.', 'NotSupportedError')
    }
    const attempt = {}
    screenRequest = attempt
    let picked
    try {
      // The first browser call after the app's control arrives: the click in
      // the app frame that sent it also activated this shell, and the browser
      // opens its picker only while that activation lasts.
      picked = mediaDevices.getDisplayMedia(displayConstraints())
    } catch (error) {
      picked = Promise.reject(error)
    }
    Promise.resolve(picked).then((stream) => {
      if (screenRequest !== attempt || phase !== 'live') {
        // Withdrawn, or the call ended, while the picker was open.
        stopStream(stream)
        return
      }
      screenRequest = null
      beginScreen(stream)
    }, (error) => {
      if (screenRequest !== attempt) return
      screenRequest = null
      report(null, screenFailure(error))
    })
  }

  function apply(action, value) {
    try {
      if (action === 'connect') connect(value)
      else if (action === 'signal') signal(value)
      else if (action === 'disconnect') disconnect(value)
      else if (action === 'volume') setVolumes(value)
      else if (action === 'tiles') setTiles(value)
      else if (action === 'local') setLocal(value)
      else if (action === 'screen') shareScreen(value)
    } catch (error) {
      // Per-peer and payload problems never end the call.
      report(isPlainObject(value) ? readPeer(value.peer) : null, error)
    }
  }

  function teardown() {
    if (phase === 'ended') return
    phase = 'ended'
    pending.length = 0
    if (levelTimer != null) {
      try { stopInterval(levelTimer) } catch { /* already cleared */ }
      levelTimer = null
    }
    for (const peer of peers.values()) closePeer(peer)
    peers.clear()
    gains.clear()
    tiles = []
    if (surface) {
      try { surface.destroy() } catch { /* elements already removed */ }
      surface = null
    }
    if (selfMeter) disconnectNodes(selfMeter.source, selfMeter.analyser)
    selfMeter = null
    // A device that is granted after this is stopped when it arrives.
    deviceRequest = null
    stopStream(localStream)
    localStream = null
    localAudio = null
    localVideo = null
    selfVideo = null
    // Closing each connection removed the screen's senders; a picker still
    // open settles into a stopped track.
    screenRequest = null
    stopStream(screenStream)
    screenTrack = null
    screenStream = null
    audioContext.onstatechange = null
    try {
      Promise.resolve(audioContext.close?.()).catch(() => {})
    } catch { /* already closed */ }
  }

  function finish() {
    if (phase === 'ended') return
    const durationMs = startedAt == null ? 0 : Math.max(0, Math.round(now() - startedAt))
    teardown()
    channel.result({ durationMs })
  }

  function cancel() {
    if (phase === 'ended') return
    teardown()
    // Settle after the host's own abort: a detached frame or changed contract
    // reports its own reason; this settles deactivation and app cancellation.
    Promise.resolve().then(() => channel.error(callError(
      'aborted',
      'The call ended because it was cancelled or this app is no longer visible.',
      'AbortError',
    )))
  }

  function control(action, value) {
    if (phase === 'ended') return
    if (action === 'finish') {
      finish()
    } else if (action === 'cancel') {
      cancel()
    } else if (action === 'audio-resume') {
      resumeAudio()
    } else if (!CONTROLS.has(action)) {
      report(null, invalid(`Unknown media.call control \`${String(action).slice(0, 40)}\`.`))
    } else if (phase === 'starting') {
      // Peers attach local tracks, so connection work waits for the devices.
      if (pending.length >= MAX_PENDING_CONTROLS) {
        report(null, callError('limit_exceeded', 'Too many call controls arrived before the call was ready.'))
      } else {
        pending.push([action, value])
      }
    } else {
      apply(action, value)
    }
  }

  audioContext.onstatechange = notePlayback
  // The app opens a call from its own user gesture, and that activation also
  // reaches this ancestor shell, so an autoplay-suspended context can start.
  resumeAudio()
  Promise.resolve().then(acquireMedia).then(begin).catch(abortStart)
  return { control }
}

/**
 * `media.call` v1 provider. `mediaDevices` supplies both `getUserMedia` and
 * `getDisplayMedia`. `createSurface()` returns the host tile painter
 * (`{ paint(tiles), destroy() }`) for the frame that owns the session.
 */
export function createCallProvider({
  mediaDevices = globalThis.navigator?.mediaDevices,
  RTCPeerConnectionCtor = globalThis.RTCPeerConnection,
  MediaStreamCtor = globalThis.MediaStream,
  AudioContextCtor = globalThis.AudioContext || globalThis.webkitAudioContext,
  createElement = (tag) => globalThis.document?.createElement?.(tag) || null,
  createSurface = null,
  now = () => globalThis.performance?.now?.() ?? Date.now(),
  setInterval: startInterval = (callback, ms) => globalThis.setInterval(callback, ms),
  clearInterval: stopInterval = (id) => globalThis.clearInterval(id),
} = {}) {
  const environment = {
    mediaDevices, RTCPeerConnectionCtor, MediaStreamCtor, AudioContextCtor,
    createElement, createSurface, now, startInterval, stopInterval,
  }
  return {
    version: 1,
    // One call per app frame; a second open while one is live is `busy`.
    exclusive: true,
    // A hidden app leaves the call; there is no partial result worth keeping.
    onDeactivate: 'cancel',
    open({ input, declaration, channel }) {
      return startCall(readCallRequest(input, declaration), channel, environment)
    },
  }
}
