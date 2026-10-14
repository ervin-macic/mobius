/* What fills a chat's context: Möbius's measured parts against the provider's total. */

/**
 * The parts Möbius adds to every chat, in display order. Keys are the backend's
 * `/context-sizes` contract (app/agent_context.py); a part the backend adds
 * later stays inside "Conversation and other" until it gets a row here.
 */
export const CONTEXT_PARTS = [
  {
    key: 'instructions',
    label: 'Möbius instructions',
    description: 'How the agent works, its safety rules, and what installed apps add',
  },
  {
    key: 'skills',
    label: 'Skills list',
    description: 'The name and one-line description of each skill it can read',
  },
  {
    key: 'recent_chats',
    label: 'Recent chat summaries',
    description: 'Your latest chats, a short summary each',
  },
  {
    key: 'app_context',
    label: 'App context',
    description: 'The app or project this chat belongs to',
  },
  {
    key: 'compaction',
    label: 'Compaction summary',
    description: 'The handoff written when this chat was last compacted',
  },
]

/** From this share of the window on, /context offers to compact the chat. */
export const COMPACT_SUGGESTION_PERCENT = 80

function sum(parts) {
  return parts.reduce((total, part) => total + part.tokens, 0)
}

/** Whether the context is genuinely empty because the chat never ran a turn.
 *
 * A missing provider session id alone cannot mean "nothing is in context":
 * compaction and provider handoffs clear the resumable session id on chats
 * full of history (the backend documents this in side_questions.py). Only the
 * chat's explicit lifecycle signal — a provisional chat that has not sent its
 * first message — makes "nothing is in context yet" true. An established chat
 * with a cleared session id reports unknown usage instead.
 */
export function contextNeverStarted({ started = true, providerSessionId = null } = {}) {
  return !started && !providerSessionId
}

/**
 * Rows that add up, from the measured parts and the provider's reading.
 *
 * `parts` is the backend's `[{ key, tokens }]` estimate; `tokens` is
 * `{ used, maximum }` (useContextTokens) or null when unknown; `noSession`
 * means the chat has not started yet. Returns one of:
 *
 * - `{ state: 'not_started', parts }`: nothing is in context; the parts load
 *   with the first reply.
 * - `{ state: 'unknown', parts }`: the provider's total is not available, so
 *   only Möbius's own parts can be shown.
 * - `{ state: 'measured', used, maximum, percent, parts, conversation, free,
 *   scaled }`: parts + conversation === used and used + free === maximum.
 *
 * Providers report one total; Möbius estimates its own parts, and the rest is
 * the conversation plus what Möbius cannot see (the provider's own
 * instructions and tools). Estimates can overshoot a small total, so they are
 * then scaled down to fit it (`scaled`): the conversation is never negative.
 */
export function contextBreakdown({ parts, tokens, noSession = false }) {
  const sizes = new Map(
    (Array.isArray(parts) ? parts : []).map(part => [
      part?.key,
      Math.max(0, Math.round(Number(part?.tokens) || 0)),
    ]),
  )
  const measured = CONTEXT_PARTS
    .map(part => ({ ...part, tokens: sizes.get(part.key) ?? 0 }))
    .filter(part => part.tokens > 0)
  if (noSession) return { state: 'not_started', parts: measured }
  if (!tokens) return { state: 'unknown', parts: measured }

  const maximum = Math.max(0, Math.round(tokens.maximum))
  const used = Math.min(Math.max(0, Math.round(tokens.used)), maximum)
  const estimated = sum(measured)
  const scale = estimated > used ? used / estimated : 1
  // Flooring keeps the scaled parts at or under `used`; the remainder (never
  // negative) is the conversation.
  const fitted = measured.map(part => ({ ...part, tokens: Math.floor(part.tokens * scale) }))
  return {
    state: 'measured',
    used,
    maximum,
    percent: maximum > 0 ? Math.round((used / maximum) * 100) : 0,
    parts: fitted,
    conversation: used - sum(fitted),
    free: maximum - used,
    scaled: scale < 1,
  }
}

/** Whether /context should offer to compact: only when the context is nearly full. */
export function suggestsCompaction(breakdown) {
  return breakdown?.state === 'measured' && breakdown.percent >= COMPACT_SUGGESTION_PERCENT
}
