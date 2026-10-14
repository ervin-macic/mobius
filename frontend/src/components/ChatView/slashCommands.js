/* Slash-command registry, fuzzy matching, and menu key resolution for the composer. */

/**
 * The composer's "/" menu.
 *
 * DISPATCH AUTHORITY LIVES IN THE BACKEND. Möbius owns every command here;
 * none is forwarded to a provider CLI. `/goal` creates the Möbius Goal and the
 * agent receives its objective as a plain request, so Claude's own `/goal`
 * never runs. The backend's MOBIUS_SLASH_COMMANDS (backend/app/chat_context.py)
 * is the dispatch half of this list; this registry is the PRESENTATION half.
 *
 * Listing a command here that the backend does not dispatch is the failure
 * worth guarding: the menu would offer it, the user would pick it, and it
 * would silently degrade into ordinary prose with no error anywhere. The two
 * lists are pinned together by a backend parity test
 * (test_slash_command_registry_parity) that reads THIS file, so adding a
 * command in one place and not the other fails the suite rather than shipping
 * a dead entry.
 *
 * `providers` records where a command actually does something. Both current
 * runners expose Möbius's durable goal controls, so `/goal` is available in
 * Claude and Codex chats. The picker
 * still shows commands that are unavailable in the current chat, with a clear
 * explanation, so typing "/" never looks broken merely because the provider
 * changed. Availability is enforced separately from matching so an
 * unavailable row can be discoverable without ever being accepted.
 */
export const SLASH_COMMANDS = [
  {
    name: 'goal',
    args: '<what to keep working toward>',
    summary: 'Keep pursuing a goal across turns',
    detail: 'Runs until the goal holds. Clear it from the Goal rail.',
    providers: ['claude', 'codex'],
  },
  {
    name: 'compact',
    summary: 'Compact this chat',
    detail: 'Refresh live context; add guidance after the command. The transcript stays.',
  },
  {
    // `aliases` are other names typed for the same command. The menu shows
    // one row, and the parity test counts each alias as a dispatched command.
    name: 'new',
    aliases: ['clear'],
    args: '<first message>',
    summary: 'Start a new chat',
    detail: 'Same project and model. Any text after it becomes the first message.',
  },
  {
    name: 'context',
    summary: 'See what fills the context',
    detail: 'Estimated size of each part, from Möbius instructions to the conversation.',
  },
  {
    name: 'btw',
    args: '<side question>',
    summary: 'Ask a side question',
    detail: 'A temporary copy of this chat answers. The agent isn’t interrupted.',
    // Answers come from a fork of the provider's own session. Claude-only:
    // only a Claude fork can run with every tool turned off, and a copy that
    // kept file reads could leak owner data to the provider (PR #1836 review).
    providers: ['claude'],
  },
]

/**
 * Commands that are chat actions: the composer runs them instead of sending.
 *
 * `/goal` is not here: it is sent as a message, and the backend turns it into
 * the Goal and hands the agent a plain request. These never become prose:
 * `/compact` rewrites the chat's live context and resets the provider
 * session; `/new` (typed `/clear` too) opens a new chat in the same project;
 * `/context` opens the context breakdown; `/btw` asks a temporary copy of the
 * chat. The registry above is still pinned to the backend by
 * test_slash_command_registry_parity, so a menu entry can never silently
 * degrade into a message.
 */
const CHAT_ACTIONS = {
  '/compact': 'compact',
  '/new': 'new',
  '/clear': 'new',
  '/context': 'context',
  '/btw': 'btw',
}

export const MOBIUS_CHAT_COMMANDS = Object.keys(CHAT_ACTIONS)

/**
 * The chat action typed in the composer, or null for anything to send.
 *
 * `name` is what was typed (`/clear` stays `/clear`), `action` is what runs
 * (`new` for both), and `argument` is the trimmed text after the command:
 * compaction guidance, a new chat's first message, or the side question.
 */
export function mobiusChatCommand(text) {
  const value = (text ?? '').trim()
  if (!value) return null
  const match = value.match(/^(\/[a-z]+)(?:\s+([\s\S]*))?$/)
  const action = match ? CHAT_ACTIONS[match[1]] : null
  if (!action) return null
  return { name: match[1], action, argument: (match[2] || '').trim() }
}

/**
 * Why `command` cannot run as typed, or null when it can. `/context` opens
 * the context breakdown and takes no text: running it anyway would silently
 * discard the trailing words, so the composer keeps them and shows this.
 * A command typed in full skips the menu, so the menu's provider rule is
 * enforced here too: `/btw` on a Codex chat is refused before any request.
 */
export function chatCommandRejection(command, provider) {
  if (!command) return null
  const entry = SLASH_COMMANDS.find((candidate) => candidate.name === command.action)
  if (entry && !slashCommandIsAvailable(entry, provider)) {
    return `‘${command.name}’ isn’t available here. ${slashCommandUnavailableReason(entry, provider)} Your message stays in the box.`
  }
  if (command.action === 'context' && command.argument) {
    return '‘/context’ takes no text after it. Your message stays in the box.'
  }
  return null
}

/** Restore a failed compact command unless the owner has already typed anew. */
export function compactFailureInput(currentInput, submittedInput) {
  return currentInput || submittedInput
}

/**
 * The command fragment being typed, or null when the composer isn't picking one.
 *
 * Slash mode is narrow on purpose. It opens on a leading "/" and closes the
 * moment the command is committed — the first space means the user has moved
 * on to writing arguments, and a menu hovering over that is noise. It also
 * declines anything holding a second "/", so a path typed as the first word
 * ("/data/apps/x is broken") never summons a command menu; the backend's
 * dispatch check refuses those for the same reason.
 */
export function slashQueryFor(input) {
  const text = input ?? ''
  if (!text.startsWith('/')) return null
  const rest = text.slice(1)
  if (/[\s/]/.test(rest)) return null
  return rest
}

/**
 * Subsequence score for `query` against a command name, or null for no match.
 *
 * Plain subsequence matching alone ranks badly once there are several
 * commands: every candidate containing the letters "scattered anywhere" ties
 * with the one the user obviously meant. So matches score higher when they run
 * consecutively, when they land early, and when they start the name — "go"
 * should put "goal" above a hypothetical "diagnose".
 */
export function fuzzyScore(query, name) {
  const q = (query ?? '').toLowerCase()
  const n = (name ?? '').toLowerCase()
  if (!q) return 0
  let score = 0
  let from = 0
  let previous = -2
  let streak = 0
  for (const char of q) {
    const hit = n.indexOf(char, from)
    if (hit === -1) return null
    streak = hit === previous + 1 ? streak + 1 : 0
    score += 10 + streak * 5 - Math.min(hit - from, 6)
    if (hit === 0) score += 8
    previous = hit
    from = hit + 1
  }
  return score
}

/** The command's best-scoring name or alias for `query`, or null. */
function bestNameMatch(query, command) {
  let best = null
  for (const typed of [command.name, ...(command.aliases || [])]) {
    const score = fuzzyScore(query, typed)
    if (score !== null && (best === null || score > best.score)) best = { score, typed }
  }
  return best
}

/**
 * Commands offered for the current composer text, best match first.
 *
 * Returns [] when the composer isn't in slash mode, which is also the signal
 * to keep the menu closed — callers don't need a second predicate. A command
 * matched through an alias comes back as a copy carrying `invokedAs`, so the
 * menu shows and accepting inserts the name being typed ("/cl" → "/clear").
 */
export function matchSlashCommands(input) {
  const query = slashQueryFor(input)
  if (query === null) return []
  return SLASH_COMMANDS
    .map((command) => ({ command, match: bestNameMatch(query, command) }))
    .filter((entry) => entry.match !== null)
    .sort((a, b) => b.match.score - a.match.score || a.command.name.localeCompare(b.command.name))
    .map(({ command, match }) => (
      match.typed === command.name ? command : { ...command, invokedAs: match.typed }
    ))
}

/** Whether selecting this command would dispatch on the current provider. */
export function slashCommandIsAvailable(command, provider) {
  return !command.providers?.length
    || (typeof provider === 'string' && command.providers.includes(provider))
}

/** Short user-facing reason for a visible but unavailable command. */
export function slashCommandUnavailableReason(command, provider) {
  if (slashCommandIsAvailable(command, provider)) return ''
  if (typeof provider !== 'string') return 'Available after this chat finishes loading.'
  const providers = command.providers.map(
    (name) => name.charAt(0).toUpperCase() + name.slice(1),
  )
  return `Available in ${providers.join(' or ')} chats.`
}

/** Focus and dismissal are the two UI facts that may hide valid matches. */
export function visibleSlashCommands(commands, { focused, dismissed } = {}) {
  return focused && !dismissed ? commands : []
}

/**
 * What an open menu should do with a keydown, or null to leave the key alone.
 *
 * Kept pure and separate from the bar's other key handling because the menu
 * has to win Enter and ArrowUp/ArrowDown while it is open — the same keys that
 * otherwise send the message and walk sent-message history. Returning null for
 * every key the menu doesn't claim is what lets the caller fall through to
 * that existing behaviour untouched.
 */
export function resolveSlashMenuKey(event, { open, count } = {}) {
  if (!open || !count) return null
  if (event.metaKey || event.ctrlKey || event.altKey) return null
  if (event.key === 'ArrowDown') return 'next'
  if (event.key === 'ArrowUp') return 'previous'
  if (event.key === 'Escape') return 'dismiss'
  // Tab completes without sending — the conventional "fill it in" key, and the
  // only way to accept a command when it is the sole thing typed on a touch
  // keyboard that has no Enter-vs-send distinction.
  if (event.key === 'Tab' && !event.shiftKey) return 'accept'
  if (event.key === 'Enter' && !event.shiftKey) return 'accept'
  return null
}

/** Composer text after accepting a command: the name plus the space its arguments start after. */
export function applySlashCommand(command) {
  return `/${command.invokedAs || command.name} `
}
