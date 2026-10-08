/**
 * Input checks for `media.call` v1: the open request, signalling payloads, the
 * screen control, and tile geometry an app may send. Everything the app passes
 * is validated here before it reaches a browser media API, and only plain
 * copies holding known fields leave these functions. The session itself lives
 * in callSession.js.
 */

export const SELF = 'self'
const TILE_SOURCES = ['camera', 'screen']
const PEER_ID = /^[A-Za-z0-9_.:~@-]{1,80}$/
const ICE_URL = /^(?:stun|turns?):\S+$/
const DEFAULT_MAX_PEERS = 8
const HARD_MAX_PEERS = 32
const MAX_ICE_SERVERS = 4
const MAX_ICE_URLS = 4
const MAX_ICE_URL_CHARS = 512
const MAX_ICE_SECRET_CHARS = 256
const MAX_SDP_CHARS = 100 * 1024
const MAX_CANDIDATE_CHARS = 2 * 1024
const MAX_CANDIDATE_FIELD_CHARS = 256
const MAX_TILE_COORDINATE = 100_000

export function callError(code, message, name = 'CapabilityError') {
  const error = new Error(message)
  error.name = name
  error.code = code
  return error
}

export function invalid(message) {
  return callError('invalid_request', message, 'TypeError')
}

export function isPlainObject(value) {
  if (!value || typeof value !== 'object') return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

export function assertFields(value, allowed, label) {
  if (!isPlainObject(value)) throw invalid(`${label} must be an object.`)
  const unknown = Object.keys(value).filter((key) => !allowed.includes(key))
  if (unknown.length) {
    const names = unknown.sort().slice(0, 4).map((key) => key.slice(0, 40))
    throw invalid(`Unknown ${label} field: ${names.join(', ')}.`)
  }
}

export function finiteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value)
}

export function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value))
}

export function readPeer(value) {
  return typeof value === 'string' && value !== SELF && PEER_ID.test(value)
    ? value
    : null
}

export function requirePeer(value) {
  const peer = readPeer(value)
  if (!peer) {
    throw invalid(
      'A call peer id must be 1-80 letters, digits, or `_.:~@-` characters, and not `self`.',
    )
  }
  return peer
}

function readIceServer(entry, index) {
  const label = `iceServers[${index}]`
  assertFields(entry, ['urls', 'username', 'credential'], label)
  const urls = typeof entry.urls === 'string' ? [entry.urls] : entry.urls
  if (
    !Array.isArray(urls)
    || urls.length < 1
    || urls.length > MAX_ICE_URLS
    || !urls.every((url) => (
      typeof url === 'string' && url.length <= MAX_ICE_URL_CHARS && ICE_URL.test(url)
    ))
  ) {
    throw invalid(
      `${label}.urls must be 1-${MAX_ICE_URLS} stun:, turn:, or turns: URLs `
        + `of at most ${MAX_ICE_URL_CHARS} characters.`,
    )
  }
  const server = { urls: [...urls] }
  for (const field of ['username', 'credential']) {
    if (entry[field] === undefined) continue
    if (typeof entry[field] !== 'string' || entry[field].length > MAX_ICE_SECRET_CHARS) {
      throw invalid(
        `${label}.${field} must be a string of at most ${MAX_ICE_SECRET_CHARS} characters.`,
      )
    }
    server[field] = entry[field]
  }
  if (
    urls.some((url) => url.startsWith('turn'))
    && (server.username === undefined || server.credential === undefined)
  ) {
    throw invalid(`${label} needs a username and credential for its TURN URLs.`)
  }
  return server
}

export function readCallRequest(input, declaration) {
  assertFields(input ?? {}, ['audio', 'video', 'iceServers'], 'media.call input')
  const audio = input?.audio ?? true
  const video = input?.video ?? false
  if (typeof audio !== 'boolean') throw invalid('Call `audio` must be true or false.')
  if (typeof video !== 'boolean') throw invalid('Call `video` must be true or false.')
  const servers = input?.iceServers ?? []
  if (!Array.isArray(servers) || servers.length > MAX_ICE_SERVERS) {
    throw invalid(`Call \`iceServers\` must be an array of at most ${MAX_ICE_SERVERS} servers.`)
  }
  const reviewed = Math.floor(Number(declaration?.limits?.max_peers))
  return {
    audio,
    video,
    iceServers: servers.map(readIceServer),
    maxPeers: Number.isFinite(reviewed) && reviewed >= 1
      ? Math.min(HARD_MAX_PEERS, reviewed)
      : DEFAULT_MAX_PEERS,
    // Only a reviewed `screen_share: 1` lets the app ask to share the screen.
    screenShare: Number(declaration?.limits?.screen_share) >= 1,
  }
}

export function readScreenControl(value) {
  assertFields(value, ['share'], 'call screen control')
  if (typeof value.share !== 'boolean') {
    throw invalid('Call `screen` needs a boolean `share`.')
  }
  return value.share
}

function optionalCandidateField(value, field) {
  if (value === undefined || value === null) return value
  if (typeof value !== 'string' || value.length > MAX_CANDIDATE_FIELD_CHARS) {
    throw invalid(`A call candidate ${field} must be a string of at most ${MAX_CANDIDATE_FIELD_CHARS} characters.`)
  }
  return value
}

export function readSignalData(data) {
  if (!isPlainObject(data)) throw invalid('Call signal `data` must be an object.')
  const keys = Object.keys(data)
  if (keys.length !== 1 || (keys[0] !== 'description' && keys[0] !== 'candidate')) {
    throw invalid('Call signal `data` must contain exactly one `description` or `candidate`.')
  }
  if (keys[0] === 'description') {
    const description = data.description
    assertFields(description, ['type', 'sdp'], 'call description')
    if (description.type !== 'offer' && description.type !== 'answer') {
      throw invalid('A call description `type` must be `offer` or `answer`.')
    }
    if (
      typeof description.sdp !== 'string'
      || !description.sdp
      || description.sdp.length > MAX_SDP_CHARS
    ) {
      throw invalid(`A call description \`sdp\` must be 1-${MAX_SDP_CHARS} characters.`)
    }
    return { description: { type: description.type, sdp: description.sdp } }
  }
  const candidate = data.candidate
  if (candidate === null) return { candidate: null }
  assertFields(
    candidate,
    ['candidate', 'sdpMid', 'sdpMLineIndex', 'usernameFragment'],
    'call candidate',
  )
  if (typeof candidate.candidate !== 'string' || candidate.candidate.length > MAX_CANDIDATE_CHARS) {
    throw invalid(`A call candidate must be a string of at most ${MAX_CANDIDATE_CHARS} characters.`)
  }
  const index = candidate.sdpMLineIndex
  if (
    index !== undefined && index !== null
    && !(Number.isInteger(index) && index >= 0 && index <= 65_535)
  ) {
    throw invalid('A call candidate `sdpMLineIndex` must be a small non-negative integer.')
  }
  const value = { candidate: candidate.candidate }
  const mid = optionalCandidateField(candidate.sdpMid, '`sdpMid`')
  if (mid !== undefined) value.sdpMid = mid
  if (index !== undefined) value.sdpMLineIndex = index
  const fragment = optionalCandidateField(candidate.usernameFragment, '`usernameFragment`')
  if (fragment !== undefined) value.usernameFragment = fragment
  return { candidate: value }
}

function readTile(tile, index) {
  const label = `tiles[${index}]`
  assertFields(
    tile,
    ['peer', 'source', 'x', 'y', 'width', 'height', 'radius', 'mirror', 'opacity'],
    label,
  )
  const peer = tile.peer === SELF ? SELF : readPeer(tile.peer)
  if (!peer) throw invalid(`${label}.peer must be \`self\` or a call peer id.`)
  const source = tile.source ?? 'camera'
  if (!TILE_SOURCES.includes(source)) {
    throw invalid(`${label}.source must be \`camera\` or \`screen\`.`)
  }
  if (![tile.x, tile.y, tile.width, tile.height].every(finiteNumber)) {
    throw invalid(`${label} needs finite x, y, width, and height numbers.`)
  }
  if (tile.width <= 0 || tile.height <= 0) {
    throw invalid(`${label} width and height must be positive.`)
  }
  for (const field of ['radius', 'opacity']) {
    if (tile[field] !== undefined && !finiteNumber(tile[field])) {
      throw invalid(`${label}.${field} must be a finite number.`)
    }
  }
  if (tile.mirror !== undefined && typeof tile.mirror !== 'boolean') {
    throw invalid(`${label}.mirror must be true or false.`)
  }
  return {
    peer,
    source,
    x: clamp(tile.x, -MAX_TILE_COORDINATE, MAX_TILE_COORDINATE),
    y: clamp(tile.y, -MAX_TILE_COORDINATE, MAX_TILE_COORDINATE),
    width: Math.min(tile.width, MAX_TILE_COORDINATE),
    height: Math.min(tile.height, MAX_TILE_COORDINATE),
    radius: clamp(tile.radius ?? 0, 0, MAX_TILE_COORDINATE),
    // Only the self camera mirrors by default; a shared screen reads as-is.
    mirror: tile.mirror ?? (peer === SELF && source === 'camera'),
    opacity: clamp(tile.opacity ?? 1, 0, 1),
  }
}

export function readTiles(value, maxTiles) {
  assertFields(value, ['tiles'], 'call tiles control')
  if (!Array.isArray(value.tiles)) throw invalid('Call `tiles` must be an array.')
  if (value.tiles.length > maxTiles) {
    throw callError('limit_exceeded', `At most ${maxTiles} call tiles can be painted.`, 'RangeError')
  }
  return value.tiles.map(readTile)
}
