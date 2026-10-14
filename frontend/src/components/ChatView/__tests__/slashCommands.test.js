import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  MOBIUS_CHAT_COMMANDS,
  SLASH_COMMANDS,
  applySlashCommand,
  chatCommandRejection,
  compactFailureInput,
  mobiusChatCommand,
  fuzzyScore,
  matchSlashCommands,
  resolveSlashMenuKey,
  slashCommandIsAvailable,
  slashCommandUnavailableReason,
  slashQueryFor,
  visibleSlashCommands,
} from '../slashCommands.js'

test('a leading slash opens the menu and the typed fragment is the query', () => {
  assert.equal(slashQueryFor('/'), '')
  assert.equal(slashQueryFor('/go'), 'go')
  assert.equal(slashQueryFor('/goal'), 'goal')
})

test('the menu closes once the command is committed and arguments begin', () => {
  assert.equal(slashQueryFor('/goal '), null)
  assert.equal(slashQueryFor('/goal ship the thing'), null)
})

test('a path typed as the first word is not a command', () => {
  // The backend dispatch check declines these for the same reason; a menu
  // popping up over "/data/apps/x is broken" would be pure noise.
  assert.equal(slashQueryFor('/data/apps/x'), null)
  assert.equal(slashQueryFor('/data/apps/x is broken'), null)
})

test('a slash anywhere but the start is ordinary prose', () => {
  assert.equal(slashQueryFor('please run /goal later'), null)
  assert.equal(slashQueryFor(''), null)
})

test('fuzzy matching accepts subsequences and rejects absent letters', () => {
  assert.ok(fuzzyScore('gl', 'goal') !== null)
  assert.ok(fuzzyScore('goal', 'goal') !== null)
  assert.equal(fuzzyScore('gz', 'goal'), null)
})

test('contiguous matches at the start outrank scattered ones', () => {
  assert.ok(fuzzyScore('go', 'goal') > fuzzyScore('ol', 'goal'))
})

test('an empty query offers every command regardless of provider', () => {
  assert.deepEqual(
    matchSlashCommands('/').map((c) => c.name).sort(),
    SLASH_COMMANDS.map((c) => c.name).sort(),
  )
})

test('goal is selectable on Claude', () => {
  const [goal] = matchSlashCommands('/go')
  assert.equal(goal.name, 'goal')
  assert.equal(slashCommandIsAvailable(goal, 'claude'), true)
  assert.equal(slashCommandUnavailableReason(goal, 'claude'), '')
})

test('goal is selectable on Codex, which exposes the same durable goal controls', () => {
  const [goal] = matchSlashCommands('/go')
  assert.equal(slashCommandIsAvailable(goal, 'codex'), true)
  assert.equal(slashCommandUnavailableReason(goal, 'codex'), '')
})

test('provider-specific commands fail closed while provider metadata loads', () => {
  const [goal] = matchSlashCommands('/go')
  assert.equal(goal.name, 'goal')
  assert.equal(slashCommandIsAvailable(goal, undefined), false)
  assert.equal(
    slashCommandUnavailableReason(goal, undefined),
    'Available after this chat finishes loading.',
  )
})

test('matching commands are visible only while the composer owns focus', () => {
  const commands = matchSlashCommands('/go')
  assert.equal(visibleSlashCommands(commands, { focused: true }).length, 1)
  assert.deepEqual(visibleSlashCommands(commands, { focused: false }), [])
  assert.deepEqual(visibleSlashCommands(commands, {
    focused: true,
    dismissed: true,
  }), [])
})

test('a non-matching query offers nothing', () => {
  assert.deepEqual(matchSlashCommands('/zzz'), [])
})

test('the menu claims its keys only while open', () => {
  const open = { open: true, count: 1 }
  assert.equal(resolveSlashMenuKey({ key: 'ArrowDown' }, open), 'next')
  assert.equal(resolveSlashMenuKey({ key: 'ArrowUp' }, open), 'previous')
  assert.equal(resolveSlashMenuKey({ key: 'Enter' }, open), 'accept')
  assert.equal(resolveSlashMenuKey({ key: 'Tab' }, open), 'accept')
  assert.equal(resolveSlashMenuKey({ key: 'Escape' }, open), 'dismiss')
})

test('a closed or empty menu leaves every key to the composer', () => {
  // Otherwise the menu would swallow Enter and the history arrows when there
  // is nothing on screen to act on.
  assert.equal(resolveSlashMenuKey({ key: 'Enter' }, { open: false, count: 1 }), null)
  assert.equal(resolveSlashMenuKey({ key: 'Enter' }, { open: true, count: 0 }), null)
})

test('shift and modifier chords stay with the composer', () => {
  const open = { open: true, count: 1 }
  // Shift+Enter is newline; Cmd/Ctrl+Enter is the steer-submit shortcut.
  assert.equal(resolveSlashMenuKey({ key: 'Enter', shiftKey: true }, open), null)
  assert.equal(resolveSlashMenuKey({ key: 'Enter', metaKey: true }, open), null)
  assert.equal(resolveSlashMenuKey({ key: 'Enter', ctrlKey: true }, open), null)
})

test('accepting a command leaves the caret where its arguments start', () => {
  assert.equal(applySlashCommand({ name: 'goal' }), '/goal ')
  // And the completed text is out of slash mode, so the menu closes itself.
  assert.equal(slashQueryFor(applySlashCommand({ name: 'goal' })), null)
})

test('/compact is offered and recognised as a platform-handled command', () => {
  const offered = matchSlashCommands('/comp')
  assert.deepEqual(offered.map((command) => command.name), ['compact'])
  assert.ok(slashCommandIsAvailable(offered[0], 'mobius'))
  assert.ok(slashCommandIsAvailable(offered[0], 'codex'))

  assert.deepEqual(mobiusChatCommand('/compact'), {
    name: '/compact', action: 'compact', argument: '',
  })
  assert.deepEqual(mobiusChatCommand('  /compact  '), {
    name: '/compact', action: 'compact', argument: '',
  })
  assert.deepEqual(mobiusChatCommand('/compact focus on the UI decisions'), {
    name: '/compact', action: 'compact', argument: 'focus on the UI decisions',
  })
  assert.equal(mobiusChatCommand('/compacted'), null)
  assert.equal(mobiusChatCommand('please /compact'), null)
  assert.equal(mobiusChatCommand(''), null)
})

test('accepting /compact composes the exact command the dispatcher matches', () => {
  const [compact] = matchSlashCommands('/compact')
  assert.deepEqual(mobiusChatCommand(applySlashCommand(compact)), {
    name: '/compact', action: 'compact', argument: '',
  })
})

test('every chat action is in the menu, and only /goal is sent as a message', () => {
  const offered = new Set(SLASH_COMMANDS.flatMap(command => [
    `/${command.name}`,
    ...(command.aliases || []).map(alias => `/${alias}`),
  ]))
  for (const name of MOBIUS_CHAT_COMMANDS) assert.ok(offered.has(name), name)
  assert.deepEqual(
    [...offered].filter(name => !MOBIUS_CHAT_COMMANDS.includes(name)),
    ['/goal'],
  )
})

test('/new and /clear open a new chat and carry the text after them as its first message', () => {
  assert.deepEqual(mobiusChatCommand('/new'), { name: '/new', action: 'new', argument: '' })
  assert.deepEqual(mobiusChatCommand('/clear Fix the colour dots on phones'), {
    name: '/clear', action: 'new', argument: 'Fix the colour dots on phones',
  })
  // Line breaks in the first message are kept.
  assert.equal(
    mobiusChatCommand('/new first line\nsecond line').argument,
    'first line\nsecond line',
  )
})

test('words that only begin like a chat command are sent as ordinary messages', () => {
  for (const text of ['/newer idea', '/clearly not', '/contextual', '/btwx', '/data/apps/x']) {
    assert.equal(mobiusChatCommand(text), null, text)
  }
})

test('/context and /btw are chat actions; /btw carries its question', () => {
  assert.deepEqual(mobiusChatCommand('/context'), {
    name: '/context', action: 'context', argument: '',
  })
  assert.deepEqual(mobiusChatCommand('/btw what do the Kanban tests cover?'), {
    name: '/btw', action: 'btw', argument: 'what do the Kanban tests cover?',
  })
})

test('/context with trailing text is rejected with an explanation, not discarded', () => {
  const command = mobiusChatCommand('/context why is this full?')
  assert.equal(command.action, 'context')
  assert.match(chatCommandRejection(command, 'claude'), /takes no text/,
    'running it anyway would silently discard the trailing words')
  assert.equal(chatCommandRejection(mobiusChatCommand('/context'), 'claude'), null)
  assert.equal(chatCommandRejection(mobiusChatCommand('/btw why is this full?'), 'claude'), null,
    'commands that take text keep taking it')
  assert.equal(chatCommandRejection(null), null)
})

test('a command typed in full is refused where the menu would mark it unavailable', () => {
  const btw = mobiusChatCommand('/btw what runs?')
  assert.match(chatCommandRejection(btw, 'codex'), /‘\/btw’ isn’t available here\. Available in Claude chats\./,
    'a hand-typed /btw on a Codex chat must not reach a server that refuses it')
  assert.match(chatCommandRejection(btw, undefined), /after this chat finishes loading/)
  assert.equal(chatCommandRejection(btw, 'claude'), null)
  for (const text of ['/compact', '/new', '/clear', '/context']) {
    assert.equal(chatCommandRejection(mobiusChatCommand(text), 'codex'), null,
      `${text} runs on every provider`)
  }
})

test('typing an alias offers its command under the alias and accepting keeps it', () => {
  const [command] = matchSlashCommands('/cl')
  assert.equal(command.name, 'new')
  assert.equal(command.invokedAs, 'clear')
  assert.equal(applySlashCommand(command), '/clear ')
  assert.deepEqual(mobiusChatCommand(applySlashCommand(command)), {
    name: '/clear', action: 'new', argument: '',
  })
})

test('the bare menu lists /new once, under its own name', () => {
  const offered = matchSlashCommands('/')
  assert.equal(offered.filter(command => command.name === 'new').length, 1)
  const [newCommand] = offered.filter(command => command.name === 'new')
  assert.equal(newCommand.invokedAs, undefined)
  assert.equal(applySlashCommand(newCommand), '/new ')
})

test('/btw is selectable only in Claude chats, whose forks run tool-less', () => {
  const [btw] = matchSlashCommands('/btw')
  assert.equal(btw.name, 'btw')
  assert.equal(slashCommandIsAvailable(btw, 'claude'), true)
  assert.equal(slashCommandIsAvailable(btw, 'codex'), false)
  assert.equal(slashCommandIsAvailable(btw, 'mobius'), false)
  assert.equal(
    slashCommandUnavailableReason(btw, 'codex'),
    'Available in Claude chats.',
  )
})

test('a failed compact restores its guidance without overwriting new typing', () => {
  const submitted = '/compact keep the architecture decisions'
  assert.equal(compactFailureInput('', submitted), submitted)
  assert.equal(compactFailureInput('new draft', submitted), 'new draft')
})
