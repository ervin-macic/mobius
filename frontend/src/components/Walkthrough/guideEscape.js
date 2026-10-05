/* Escape dismisses the guide for good, so it must not fire while the owner is backing out of a text
   field (for example the handle field) or confirming an IME composition. */
const TEXT_ENTRY = 'input:not([type="button"]):not([type="submit"]):not([type="checkbox"]):not([type="radio"]), textarea, select, [contenteditable=""], [contenteditable="true"]'

export function escapeShouldDismissGuide(event) {
  if (event?.isComposing || event?.keyCode === 229) return false
  return !event?.target?.closest?.(TEXT_ENTRY)
}
