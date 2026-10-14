const APP_STORE_MANIFEST_PREFIX = 'https://raw.githubusercontent.com/mobius-os/app-store/'

export function appCrashReportDraft(name, error) {
  const detail = String(error ?? '').trim()
    || 'Unknown app error (no message reached the shell)'
  return `The app "${name}" crashed with this error:\n\`\`\`\n${detail}\n\`\`\`\nPlease investigate and fix.`
}

// A running frame keeps the builder chat id it was served with, even after
// settings clear the app's chat. The current app row decides; the frame's id
// is only a fallback when the shell has no row for the app.
export function appReportChatId(app, frameChatId) {
  return (app ? app.chat_id : frameChatId) || null
}

export function findAppStoreApp(apps) {
  let nameFallback = null
  for (const app of Array.isArray(apps) ? apps : []) {
    if (String(app?.manifest_url || '').startsWith(APP_STORE_MANIFEST_PREFIX)) {
      return app
    }
    if (nameFallback == null && app?.name === 'App Store') nameFallback = app
  }
  return nameFallback
}
