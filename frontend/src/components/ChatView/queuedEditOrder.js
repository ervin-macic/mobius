/**
 * Orders the writes to one queued message around its edits.
 *
 * Saving an edit is a round trip whose response hydrates the queue from server
 * truth. Anything else done to the same message while that save is in flight
 * would race it: a cancellation could be undone when the late response puts
 * the row back in the tray, and Send now could send the text the edit is
 * replacing. So EVERY operation on one message — each edit, its cancellation,
 * its Send now — runs through the same per-message chain, one after another.
 * A terminal action waits out the edits before it, and an edit opened during
 * a slow cancellation or Send now waits for that outcome instead of racing
 * it. Messages never wait for each other. Actions on the whole queue (Steer
 * all, Stop) wait for every message's chain through `settled`.
 */
export function createQueuedEditOrder() {
  // cid -> a promise that settles once every operation started so far settled.
  const tails = new Map()

  return {
    /** Run one operation on a queued message after its earlier operations
     * settle. Resolves or rejects exactly as `operation()` does. */
    run(cid, operation) {
      const result = (tails.get(cid) || Promise.resolve()).then(operation)
      const tail = result.then(() => {}, () => {})
      tails.set(cid, tail)
      tail.then(() => {
        if (tails.get(cid) === tail) tails.delete(cid)
      })
      return result
    },
    /** Settles (never rejects) once every operation started so far on any
     * message has settled, so a whole-queue action reads saved text. */
    settled() {
      return Promise.all([...tails.values()]).then(() => {})
    },
  }
}
