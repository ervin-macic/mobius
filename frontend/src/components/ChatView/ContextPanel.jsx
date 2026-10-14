/** ContextPanel shows what fills a chat's context window (the `/context` command). */

import { useRef } from 'react'
import { ArrowRotateCw, X } from '@openai/apps-sdk-ui/components/Icon'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { chatQueries } from '../../hooks/queries.js'
import { formatRoundedTokenCount } from './brainUsage.js'
import { contextBreakdown, suggestsCompaction } from './contextBreakdown.js'
import useContextTokens from './hooks/useContextTokens.js'
import './ContextPanel.css'

const PROVIDER_LABELS = { claude: 'Claude', codex: 'Codex', mobius: 'Möbius' }

function share(tokens, maximum) {
  return maximum > 0 ? `${(tokens / maximum) * 100}%` : '0%'
}

function PartRows({ parts }) {
  return parts.map(part => (
    <li key={part.key} className="ctx-row ctx-row--part">
      <span className="ctx-row__label">{part.label}</span>
      <span className="ctx-row__value">{formatRoundedTokenCount(part.tokens)}</span>
      <span className="ctx-row__description">{part.description}</span>
    </li>
  ))
}

function Measured({ breakdown, providerLabel }) {
  const { used, maximum, percent, parts, conversation, free, scaled } = breakdown
  const mobius = parts.reduce((total, part) => total + part.tokens, 0)
  const summary = `${percent}% of the context window used: Möbius adds `
    + `${formatRoundedTokenCount(mobius)}, conversation and other `
    + `${formatRoundedTokenCount(conversation)}, ${formatRoundedTokenCount(free)} free.`
  return (
    <>
      <div className="ctx-summary">
        <span className="ctx-summary__percent">{percent}%</span>
        <span className="ctx-summary__detail">
          used · {formatRoundedTokenCount(used)} of {formatRoundedTokenCount(maximum)}
          {' '}in this {providerLabel} chat
        </span>
      </div>
      {/* Two series, so their colours stay distinct for every reader; each
          part's own size is in its row below. */}
      <div className="ctx-bar" role="img" aria-label={summary}>
        {mobius > 0 && (
          <span className="ctx-bar__fill ctx-bar__fill--mobius" style={{ flexBasis: share(mobius, maximum) }} />
        )}
        {conversation > 0 && (
          <span className="ctx-bar__fill ctx-bar__fill--conversation" style={{ flexBasis: share(conversation, maximum) }} />
        )}
        <span className="ctx-bar__free" />
      </div>
      <ul className="ctx-rows">
        <li className="ctx-row ctx-row--group">
          <span className="ctx-row__swatch ctx-row__swatch--mobius" aria-hidden="true" />
          <span className="ctx-row__label">Möbius adds</span>
          <span className="ctx-row__value">{formatRoundedTokenCount(mobius)}</span>
        </li>
        <PartRows parts={parts} />
        <li className="ctx-row ctx-row--group">
          <span className="ctx-row__swatch ctx-row__swatch--conversation" aria-hidden="true" />
          <span className="ctx-row__label">Conversation and other</span>
          <span className="ctx-row__value">{formatRoundedTokenCount(conversation)}</span>
          <span className="ctx-row__description">
            Messages, replies and tool results, plus {providerLabel}’s own instructions and tools
          </span>
        </li>
        <li className="ctx-row ctx-row--group">
          <span className="ctx-row__swatch ctx-row__swatch--free" aria-hidden="true" />
          <span className="ctx-row__label">Free</span>
          <span className="ctx-row__value">{formatRoundedTokenCount(free)}</span>
        </li>
      </ul>
      <p className="ctx__note">
        Sizes are estimates. {providerLabel} reports only the total, so Möbius measures the parts it
        adds and the rest is the conversation.
        {scaled && ' Möbius’s estimates came out above that total, so they are scaled to fit it.'}
      </p>
    </>
  )
}

/**
 * Read-only and free: no model is called. The parts come from the server's
 * estimate (`/context-sizes`), the total and the window from the same reading
 * as the composer's context gauge. `onCompact` is offered only when the
 * context is nearly full (contextBreakdown.suggestsCompaction).
 */
export default function ContextPanel({
  chatId,
  started = true,
  provider = null,
  providerSessionId = null,
  model = null,
  liveContext = null,
  onClose,
  onCompact,
}) {
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: onClose ? closeRef : dialogRef,
    onClose,
    closeOnEscape: !!onClose,
  })
  const sizes = chatQueries.contextSizes.useQuery(chatId, { enabled: started && !!chatId })
  const { tokens, noSession, loading } = useContextTokens({
    enabled: started && !!chatId,
    chatId,
    provider,
    providerSessionId,
    model,
    liveContext,
    started,
  })
  const providerLabel = PROVIDER_LABELS[provider] || 'The provider'
  const breakdown = started
    ? contextBreakdown({ parts: sizes.data?.parts, tokens, noSession })
    : { state: 'not_started', parts: [] }

  let body
  if (started && (sizes.isLoading || (loading && !noSession))) {
    body = <p className="ctx__state">Measuring this chat’s context…</p>
  } else if (sizes.isError && !sizes.data) {
    body = (
      <div className="ctx__state ctx__state--error" role="alert">
        <p>The context breakdown couldn’t load. Check your connection and try again.</p>
        <button
          type="button"
          className="ctx__retry"
          onClick={() => sizes.refetch()}
          disabled={sizes.isFetching}
        >
          <ArrowRotateCw width={16} height={16} aria-hidden="true" />
          {sizes.isFetching ? 'Retrying…' : 'Try again'}
        </button>
      </div>
    )
  } else if (breakdown.state === 'measured') {
    body = <Measured breakdown={breakdown} providerLabel={providerLabel} />
  } else {
    body = (
      <>
        <p className="ctx__state">
          {breakdown.state === 'not_started'
            ? 'Nothing is in this chat’s context yet. Möbius adds these parts with its first reply.'
            : `${providerLabel} hasn’t reported this chat’s total yet, so only the parts Möbius adds are shown.`}
        </p>
        {breakdown.parts.length > 0 && (
          <ul className="ctx-rows"><PartRows parts={breakdown.parts} /></ul>
        )}
      </>
    )
  }

  return (
    <div className="ctx__overlay" role="presentation" onClick={() => onClose?.()}>
      <div
        ref={dialogRef}
        className="ctx"
        role="dialog"
        aria-modal="true"
        aria-labelledby="ctx-title"
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="ctx__head">
          <div>
            <h2 id="ctx-title" className="ctx__title">Context</h2>
            <p className="ctx__subtitle">What fills this chat’s context window.</p>
          </div>
          {onClose && (
            <button
              ref={closeRef}
              type="button"
              className="ctx__close"
              onClick={onClose}
              aria-label="Close context"
            >
              <X width={18} height={18} aria-hidden="true" />
            </button>
          )}
        </header>
        <div className="ctx__body">{body}</div>
        {onCompact && suggestsCompaction(breakdown) && (
          <footer className="ctx__foot">
            <span>This chat’s context is nearly full.</span>
            <button type="button" className="ctx__compact" onClick={onCompact}>
              Compact now
            </button>
          </footer>
        )}
      </div>
    </div>
  )
}
