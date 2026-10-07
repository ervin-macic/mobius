// The text of an app's sidebar unread pill, or null when there is nothing to
// show. The app reports the count (PUT /api/apps/{id}/badge); the pill caps it
// so a large inbox never widens the row.
export const APP_BADGE_CAP = 99

export function appBadgeLabel(count) {
  const value = Number(count)
  if (!Number.isFinite(value) || value < 1) return null
  return value > APP_BADGE_CAP ? `${APP_BADGE_CAP}+` : String(Math.floor(value))
}
