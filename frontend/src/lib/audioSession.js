// Safari's audio session is page-wide, and WebKit ignores writes from frames
// without microphone policy, so app frames cannot choose it themselves. Under
// 'auto', Web Audio gets the ambient category that iPhone Silent Mode mutes,
// so the host selects playback while an app is mounted. Any explicit type also
// disables WebKit's automatic switch to recording, so every microphone user in
// this page must hold play-and-record while it captures. Selecting a type
// neither starts audio nor unlocks autoplay.
const holds = { playback: 0, 'play-and-record': 0 }

function selectType(session) {
  const type = holds['play-and-record'] ? 'play-and-record'
    : holds.playback ? 'playback' : 'auto'
  if (session.type !== type) session.type = type
}

export function acquireAudioSession(type, nav = globalThis.navigator) {
  const session = nav?.audioSession
  if (!session) return () => {}
  holds[type] += 1
  selectType(session)
  let held = true
  return () => {
    if (!held) return
    held = false
    holds[type] -= 1
    selectType(session)
  }
}
