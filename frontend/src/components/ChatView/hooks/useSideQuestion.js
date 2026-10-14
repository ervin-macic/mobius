/* One chat's `/btw` side question: ask a temporary copy, then keep or drop the answer. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, jsonOrThrow } from '../../../api/client.js'

/** The owner-facing reason a side question or an "Add to chat" failed. */
export function sideQuestionFailure(error) {
  // The server's own words (a 4xx/5xx detail) say what to do next, e.g.
  // "This chat has nothing to answer from yet."
  if (typeof error?.status === 'number' && error.message) return error.message
  if (error?.name === 'TimeoutError') {
    return 'The side question took too long to answer. Try again.'
  }
  return 'Couldn’t reach Möbius. Check your connection and try again.'
}

/**
 * Whether asking the same question again could succeed. A refusal of the
 * request itself (4xx: wrong provider, bad text, gone) answers the same way
 * every time, so the card offers no "Try again" for it. "Not yet" states
 * clear on their own — 409 (no reply to answer from yet, another side
 * question running), 408 and 429 — and so do server and network failures.
 */
export function sideQuestionRetryable(error) {
  const status = error?.status
  if (typeof status !== 'number' || status < 400 || status >= 500) return true
  return status === 408 || status === 409 || status === 429
}

/**
 * `sideQuestion` is null or `{ question, status, answer, provider, error, retryable }`,
 * where status is `asking`, `answered`, `adding` or `failed`. Only the latest
 * question owns the card, and closing it, asking again, changing chats or
 * unmounting ABORTS an in-flight ask, so the server can end the hidden
 * provider fork instead of running it out (its CancelledError path kills the
 * fork's process group). Nothing is saved until `add()`; a retried add reuses
 * its `cid`, so it cannot save twice. A successful add always reconciles the
 * transcript through `onAdded`, even when its card was closed or replaced
 * while the write was in flight — the row is committed either way, and hiding
 * that would misreport the write as cancelled. Only a chat switch skips the
 * refresh: `onAdded` follows the chat on screen, and the committed row is
 * read back when its own chat reopens.
 */
export default function useSideQuestion({ chatId, onAdded }) {
  const [sideQuestion, setSideQuestion] = useState(null)
  const requestRef = useRef(0)
  const controllerRef = useRef(null)
  const currentRef = useRef(null)
  currentRef.current = sideQuestion
  const onAddedRef = useRef(onAdded)
  onAddedRef.current = onAdded
  const chatIdRef = useRef(chatId)
  chatIdRef.current = chatId

  const abandon = useCallback(() => {
    requestRef.current += 1
    controllerRef.current?.abort()
    controllerRef.current = null
  }, [])

  useEffect(() => {
    abandon()
    setSideQuestion(null)
    // Unmount is a close too: never leave a hidden fork running for a card
    // that can no longer show its answer.
    return abandon
  }, [chatId, abandon])

  const ask = useCallback(async (question) => {
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    const request = ++requestRef.current
    const owns = () => requestRef.current === request
    setSideQuestion({ request, question, status: 'asking' })
    try {
      const answer = await jsonOrThrow(
        await api.chats.askSideQuestion(chatId, question, {
          signal: controller.signal,
        }),
        'Side question failed',
      )
      if (!owns()) return
      setSideQuestion({
        request,
        question: answer.question || question,
        answer: answer.answer,
        provider: answer.provider,
        cid: globalThis.crypto.randomUUID(),
        status: 'answered',
      })
    } catch (error) {
      if (!owns()) return
      setSideQuestion({
        request,
        question,
        status: 'failed',
        error: sideQuestionFailure(error),
        retryable: sideQuestionRetryable(error),
      })
    }
  }, [chatId])

  const add = useCallback(async () => {
    const card = currentRef.current
    if (card?.status !== 'answered') return
    setSideQuestion({ ...card, status: 'adding', error: null })
    try {
      await jsonOrThrow(await api.chats.keepSideQuestion(chatId, {
        cid: card.cid,
        question: card.question,
        answer: card.answer,
        provider: card.provider,
      }), 'Adding the side question failed')
      // The row is committed: show it regardless of what happened to the
      // card. After a chat switch, though, the latest `onAdded` follows the
      // chat now on screen — skip it rather than refresh the wrong chat.
      if (chatIdRef.current === chatId) onAddedRef.current?.()
      if (requestRef.current !== card.request) return
      requestRef.current += 1
      setSideQuestion(null)
    } catch (error) {
      if (requestRef.current !== card.request) return
      setSideQuestion({ ...card, status: 'answered', error: sideQuestionFailure(error) })
    }
  }, [chatId])

  const close = useCallback(() => {
    abandon()
    setSideQuestion(null)
  }, [abandon])

  const retry = useCallback(() => {
    const card = currentRef.current
    if (card?.question) void ask(card.question)
  }, [ask])

  return { sideQuestion, ask, add, close, retry }
}
