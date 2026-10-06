/* A transcript block names an app view; it carries no authority of its own.
   Opening it shows the app's live view, which applies the app's own checks to
   anything it offers to do. */

// `proposed` is a reviewed change that is not on GitHub yet, so it has no number.
const PULL_STATES = new Set(['proposed', 'open', 'draft', 'merged', 'closed'])
const TONES = new Set(['success', 'attention', 'danger', 'accent', 'neutral'])
const INTENT = /^[a-z][a-z0-9-]*:[^\s]{1,256}$/
const shortText = (value, max) => typeof value === 'string' && value.trim() ? value.trim().slice(0, max) : ''
const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null

function safeHttps(value) {
  if (typeof value !== 'string' || value.length > 2048) return undefined
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : undefined
  } catch { return undefined }
}

/* A pull-request snapshot renders like a GitHub PR row. Anything malformed is
   dropped rather than guessed, so the block falls back to its plain facts. */
function pullSnapshot(value) {
  if (!value || typeof value !== 'object') return null
  if (typeof value.repo !== 'string' || !/^[\w.-]{1,100}\/[\w.-]{1,100}$/.test(value.repo)
    || value.repo.split('/').some(part => /^\.+$/.test(part))) return null
  if (!PULL_STATES.has(value.state)) return null
  const numbered = Number.isSafeInteger(value.number) && value.number >= 1
  if (!numbered && value.state !== 'proposed') return null
  const labels = (Array.isArray(value.labels) ? value.labels : []).slice(0, 12)
    .filter(label => typeof label?.name === 'string' && label.name.trim() && label.name.length <= 50)
    .map(label => ({ name: label.name, ...(/^[0-9a-f]{6}$/i.test(label.color || '') ? { color: label.color } : {}) }))
  const badges = (Array.isArray(value.badges) ? value.badges : [])
    .filter(badge => shortText(badge?.label, 40)).slice(0, 3)
    .map(badge => ({ label: shortText(badge.label, 40), tone: TONES.has(badge.tone) ? badge.tone : 'neutral' }))
  return {
    repo: value.repo, repoUrl: `https://github.com/${value.repo}`, number: numbered ? value.number : null, state: value.state, badges,
    author: typeof value.author === 'string' && /^[\w-]{1,100}(\[bot\])?$/.test(value.author) ? value.author : null,
    files: count(value.files), additions: count(value.additions), deletions: count(value.deletions),
    labels, url: safeHttps(value.url),
  }
}

export function appBlockFromToken(token) {
  if (token?.type !== 'code' || token.lang !== 'mobius-app' || token.text?.length > 16384) return null
  try {
    const value = JSON.parse(token.text)
    if (!/^[a-z][a-z0-9-]{0,63}$/.test(value.app || '')
      || typeof value.intent !== 'string' || !INTENT.test(value.intent)
      || typeof value.title !== 'string' || !value.title.trim() || value.title.length > 240) return null
    const facts = (Array.isArray(value.facts) ? value.facts : []).slice(0, 8)
      .filter(fact => typeof fact?.label === 'string' && typeof fact?.value === 'string')
      .map(fact => {
        // Invalid source links stay plain snapshot text.
        const href = safeHttps(fact.href)
        return { label: fact.label.slice(0, 80), value: fact.value.slice(0, 240), ...(href ? { href } : {}) }
      })
    const inline = value.inline !== false
    // An app may offer one primary action (and one per batch item). It only
    // opens the app's own view with a second intent; whatever that view then
    // does is the app's to check.
    const actionOf = raw => inline && shortText(raw?.label, 40) && INTENT.test(raw?.intent || '')
      ? { label: shortText(raw.label, 40), intent: raw.intent } : null
    const action = actionOf(value.action)
    // A batch block lists up to 12 items, each its own destination, under one
    // shared action (for example "Contribute all").
    const href = intent => `/shell/?${new URLSearchParams({ app: value.app, intent })}`
    const items = (Array.isArray(value.items) ? value.items : [])
      .filter(item => shortText(item?.title, 240) && INTENT.test(item?.intent || ''))
      .slice(0, 12)
      .map(item => ({ title: item.title.trim().slice(0, 240), intent: item.intent, pull: pullSnapshot(item.pull), href: href(item.intent), action: actionOf(item.action) }))
    return { app: value.app, intent: value.intent, title: value.title, facts, pull: pullSnapshot(value.pull),
      inline, action, items,
      expandLabel: shortText(value.expand_label, 40),
      height: Math.max(240, Math.min(640, Number(value.height) || 480)),
      href: href(value.intent) }
  } catch { return null }
}
