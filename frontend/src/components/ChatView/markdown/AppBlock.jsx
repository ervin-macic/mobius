import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Branch, ChevronDown, ChevronRight } from '@openai/apps-sdk-ui/components/Icon'
import { appQueries } from '../../../hooks/queries.js'
import { sharedBrowserShellHref } from '../../../lib/sharedBrowserWorkspace.js'
import { inlineBlockState, transcriptHostRequest } from './appBlock.js'
import useAppBlockCapability from '../hooks/useAppBlockCapability.js'
import './AppBlock.css'

const AppCanvas = lazy(() => import('../../AppCanvas/AppCanvas.jsx'))

const STATE_NAMES = { proposed: 'Not sent yet', open: 'Open', draft: 'Draft', merged: 'Merged', closed: 'Closed' }
// State pills use their own tone, distinct from link styling.
const STATE_TONES = { proposed: 'neutral', open: 'success', draft: 'neutral', merged: 'success', closed: 'danger' }

/** GitHub's label treatment: tinted fill with lifted text in dark mode, the
 *  solid label color with black or white text in light mode. */
function labelStyle(hex) {
  if (!hex) return undefined
  const [r, g, b] = [0, 2, 4].map(at => parseInt(hex.slice(at, at + 2), 16))
  const max = Math.max(r, g, b) / 255, min = Math.min(r, g, b) / 255
  const l = (max + min) / 2, d = max - min
  const s = d ? d / (1 - Math.abs(2 * l - 1)) : 0
  const h = !d ? 0 : max === r / 255 ? 60 * (((g - b) / 255 / d) % 6) : max === g / 255 ? 60 * ((b - r) / 255 / d + 2) : 60 * ((r - g) / 255 / d + 4)
  const perceived = (r * 0.2126 + g * 0.7152 + b * 0.0722) / 255
  const lifted = Math.min(100, l * 100 + Math.max(0, (0.6 - perceived) * 100))
  const hsl = alpha => `hsla(${((h + 360) % 360).toFixed(1)},${(s * 100).toFixed(1)}%,${lifted.toFixed(1)}%,${alpha})`
  return {
    '--md-label-dark-bg': `rgba(${r},${g},${b},0.18)`, '--md-label-dark-fg': hsl(1), '--md-label-dark-border': hsl(0.3),
    '--md-label-light-bg': `rgb(${r},${g},${b})`, '--md-label-light-fg': perceived > 0.453 ? '#1f2328' : '#ffffff',
  }
}

/** A PR as a compact row: the title is the link into the app, details sit
 *  underneath, and the app's one action takes the bottom-right corner. */
export function PullSnapshot({ block, pull, href, open, action, session, appName, confirming = false, compact = false }) {
  const files = pull.files === null ? null : `${pull.files} ${pull.files === 1 ? 'file' : 'files'}`
  const ref = !compact && pull.number ? `${pull.repo}#${pull.number}` : pull.repo
  const status = session?.status || STATE_NAMES[pull.state]
  const statusTone = session?.status ? session.statusTone : STATE_TONES[pull.state]
  return <div className={`md-app-pull${compact ? ' md-app-pull--compact' : ''}`}>
    {compact ? <>
      <span className="md-app-pull__identity-icon" aria-hidden="true"><Branch width={18} height={18} /></span>
      <div className="md-app-pull__identity"><span>{appName || block.app}</span><span className={`md-app-pull__status is-${statusTone}`}>{status}</span></div>
    </> : null}
    <div className="md-app-pull__headline">
      <a className="md-app-pull__title" href={href} onClick={open}>{block.title}</a>
      {pull.labels.map(label => <span key={label.name} className="md-app-pull__label" style={labelStyle(label.color)}>{label.name}</span>)}
    </div>
    <div className="md-app-pull__footer">
      <div className="md-app-pull__meta">
        <a className="md-app-pull__repo" href={compact ? pull.repoUrl : pull.url || pull.repoUrl} target="_blank" rel="noopener noreferrer">{ref}</a>
        {pull.author ? <span>{pull.author}</span> : null}
        {files ? <span>{files}{pull.additions !== null ? <> <ins>+{pull.additions}</ins> <del>−{pull.deletions ?? 0}</del></> : null}</span> : null}
        {!compact ? <span className={`md-app-pull__badge is-${statusTone}`}>{status}</span> : null}
        {/* While the app asks for confirmation, only its own badges show; the
            block's saved badges are model-authored claims. */}
        {(session?.badges ?? (confirming ? [] : pull.badges)).map(badge => <span key={badge.label} className={`md-app-pull__badge is-${badge.tone}`}>{badge.label}</span>)}
      </div>
      {session?.links?.length ? <span className="md-app-block__links">{session.links.map(link => <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer">{link.label}</a>)}</span> : null}
      {action}
    </div>
    {session?.note ? <p className={`md-app-block__note is-${session.tone}`} role="status">{session.note}</p> : null}
  </div>
}

/** Host buttons for an inline session. Confirm is offered only beside the
 *  app's own summary of what it will do, never beside the block's
 *  model-authored snapshot alone. */
export function SessionControls({ target, state, appName, competingBusy = false, onEvent }) {
  if (state?.hidden) return null
  const confirming = state?.confirming === true
  const canConfirm = confirming && !!state.summary
  return <span className={`md-app-block__controls${confirming ? ' is-confirming' : ''}`}>
    {confirming ? <span className="md-app-block__confirm" role="status">
      {canConfirm ? <><strong>{appName}:</strong> {state.summary}</> : `Open ${appName} to review and confirm.`}</span> : null}
    {confirming ? <button type="button" className="md-app-block__cancel" disabled={competingBusy}
      onClick={() => onEvent(target.intent, 'cancel')}>Not now</button> : null}
    {confirming && !canConfirm ? null : <button type="button" className="md-app-block__action" disabled={state?.disabled || competingBusy}
      title={state?.label || target.label}
      aria-busy={state?.busy || undefined}
      onClick={() => onEvent(target.intent, confirming ? 'confirm' : 'activate')}>
      {state?.busy ? state.busyLabel || 'Working…' : confirming ? 'Confirm' : state?.label || target.label}</button>}
  </span>
}

/** Reuse the opaque app host; inline sessions hydrate only near the viewport. */
export default function AppBlock({ block, onInternalNav }) {
  const apps = appQueries.list.useQuery()
  const app = (apps.data || []).find(item => item.slug === block.app && !item.deleted_at)
  const canExpand = block.inline !== false
  const isSession = block.interaction === 'inline'
  const [legacyMode, setLegacyMode] = useState(null)
  const rootRef = useRef(null)
  const [nearViewport, setNearViewport] = useState(false)
  const [sessionId] = useState(() => crypto.randomUUID())
  const [blockEvent, setBlockEvent] = useState(null)
  const [sessionState, setSessionState] = useState(null)
  const [viewIntent, setViewIntent] = useState(null)
  const viewIntentRef = useRef(viewIntent)
  viewIntentRef.current = viewIntent
  const negotiating = !isSession && canExpand && viewIntent !== null
  const blockSession = useMemo(() => (isSession || negotiating) ? {
    sessionId, actions: [block.action, ...block.items.map(item => item.action)].filter(Boolean)
      .map(action => ({ key: action.intent, intent: action.intent, label: action.label })),
  } : null, [isSession, negotiating, sessionId, block])
  const allowedKeys = useMemo(() => new Set(blockSession?.actions.map(action => action.key) || []), [blockSession])
  const onSessionFallback = useCallback(key => {
    setLegacyMode('view'); setViewIntent(key); setBlockEvent(null); setSessionState(null)
  }, [])
  const { supported: blockSupported, remember: rememberBlockEvent, observe: observeBlockCapability } =
    useAppBlockCapability({ allowedKeys, onFallback: onSessionFallback })
  useEffect(() => {
    if (!isSession || !rootRef.current || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(entries => {
      if (entries[0]?.isIntersecting) setNearViewport(true)
    }, { rootMargin: '400px 0px' })
    observer.observe(rootRef.current)
    return () => observer.disconnect()
  }, [isSession])
  const onBlockState = useCallback(message => {
    const safe = inlineBlockState(message, sessionId, allowedKeys)
    if (safe) setSessionState(safe)
  }, [sessionId, allowedKeys])
  const actionState = key => sessionState?.actions.find(item => item.key === key)
  const competingBusy = sessionState?.actions.some(item => item.busy)
  const dispatchBlockEvent = useCallback((key, event) => {
    const message = { sessionId, key, event, nonce: crypto.randomUUID() }
    rememberBlockEvent(message)
    setBlockEvent(message)
  }, [sessionId, rememberBlockEvent])
  const onBlockCapability = useCallback(supported => {
    if (isSession) {
      observeBlockCapability(supported)
      return
    }
    const intent = viewIntentRef.current
    if (!intent) return
    if (supported && allowedKeys.has(intent)) {
      setLegacyMode('inline')
      dispatchBlockEvent(intent, 'activate')
    } else setLegacyMode('view')
  }, [isSession, allowedKeys, dispatchBlockEvent, observeBlockCapability])
  // The open view's intent: the block's own, or its action's. null = closed.
  const [delivered, setDelivered] = useState(false)
  const pending = useMemo(() => viewIntent ? { intent: viewIntent, nonce: crypto.randomUUID() } : null, [viewIntent])
  const href = sharedBrowserShellHref(block.href)
  const navigate = useCallback((target) => {
    const url = new URL(sharedBrowserShellHref(target), window.location.href)
    if (onInternalNav) onInternalNav(url)
    else window.location.assign(url.href)
  }, [onInternalNav])
  // Only the visible embedded view may navigate; hidden session frames mount
  // without a reader gesture.
  const visibleRef = useRef(false)
  visibleRef.current = legacyMode === 'view'
  const hostRequest = useCallback((_, request) =>
    transcriptHostRequest(request, { visible: visibleRef.current, navigate }), [navigate])
  const openHref = target => event => {
    if (!onInternalNav || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault(); navigate(target)
  }
  const open = openHref(block.href)
  const show = intent => { setViewIntent(intent); setLegacyMode(isSession && blockSupported === false ? 'view' : null); setDelivered(false); setSessionState(null); setBlockEvent(null) }
  const appName = app?.name || block.app
  const confirmingAny = sessionState?.actions.some(item => item.confirming) === true
  // The block's title, facts and repo are the conversation's snapshot; say so
  // while the app is asking for confirmation.
  const unverified = confirmingAny
    ? <p className="md-app-block__notice" role="note">{`The details shown here come from the conversation, not from ${appName}.`}</p>
    : null
  const actionButton = target => (isSession && blockSupported !== false || legacyMode === 'inline') && app && target
    ? <SessionControls target={target} state={actionState(target.intent)} appName={appName}
      competingBusy={competingBusy} onEvent={dispatchBlockEvent} />
    : canExpand && app && target
    ? <button type="button" className="md-app-block__action" aria-pressed={viewIntent === target.intent}
      onClick={() => show(viewIntent === target.intent ? null : target.intent)}>{target.label}</button>
    : null
  const action = actionButton(block.action)
  const toggle = canExpand && app && !isSession && legacyMode !== 'inline'
    ? <button type="button" className="md-app-block__toggle" aria-expanded={viewIntent !== null}
      onClick={() => show(viewIntent === null ? block.intent : null)}>
      <ChevronDown width={16} height={16} aria-hidden="true" />{viewIntent !== null ? 'Hide details' : block.expandLabel || 'Show details here'}</button>
    : null
  const view = isSession && blockSupported !== false && app && nearViewport ? <div className="md-app-block__session-host" aria-hidden="true" inert="">
    <Suspense fallback={null}><AppCanvas appId={app.id} appName={app.name} appSlug={app.slug}
      version={app.updated_at || 0} offlineCapable={app.offline_capable} capabilityContract={app.capabilities}
      active={false} visible={false} interactive={false} blockSession={blockSession} blockEvent={blockEvent}
      onBlockState={onBlockState} onBlockCapability={onBlockCapability} onHostRequest={hostRequest} /></Suspense>
  </div> : canExpand && app && pending && (!isSession || blockSupported === false) && <div className={legacyMode === 'view' ? 'md-app-block__view' : 'md-app-block__session-host'}
    style={legacyMode === 'view' ? { height: block.height } : undefined} aria-hidden={legacyMode !== 'view' ? 'true' : undefined} inert={legacyMode !== 'view' ? '' : undefined}>
    <Suspense fallback={legacyMode === 'view' ? <p role="status">Opening details…</p> : null}><AppCanvas key={viewIntent} appId={app.id} appName={app.name} appSlug={app.slug}
      version={app.updated_at || 0} offlineCapable={app.offline_capable} capabilityContract={app.capabilities}
      active={false} visible={legacyMode === 'view'} interactive={legacyMode === 'view'} pendingIntent={legacyMode === 'view' && !delivered ? pending : null}
      blockSession={blockSession} blockEvent={blockEvent} onBlockState={onBlockState} onBlockCapability={onBlockCapability}
      onIntentDelivered={() => setDelivered(true)} onHostRequest={hostRequest} /></Suspense>
  </div>
  const unavailable = canExpand && !app
    ? <p>{apps.isLoading ? 'Checking installed apps…' : `${block.app} is not available. The saved snapshot remains here.`}</p>
    : null
  if (block.items.length > 0) {
    return <section ref={rootRef} className={`md-app-block md-app-block--batch${isSession ? ' md-app-block--compact' : ''}`} aria-label={block.title}>
      <header className="md-app-batch__head"><strong>{block.title}</strong><span>{block.items.length} {block.items.length === 1 ? 'item' : 'items'}</span></header>
      <ul className="md-app-batch__list">
        {block.items.map(item => <li key={item.intent}>
          {item.pull
            ? <PullSnapshot block={item} pull={item.pull} href={sharedBrowserShellHref(item.href)} open={openHref(item.href)} action={actionButton(item.action)} session={actionState(item.action?.intent)} appName={appName} confirming={confirmingAny} compact={isSession} />
            : <a className="md-app-batch__title" href={sharedBrowserShellHref(item.href)} onClick={openHref(item.href)}>{item.title}</a>}
        </li>)}
      </ul>
      {action ? <footer className="md-app-batch__foot">
        {action}
      </footer> : null}
      {actionState(block.action?.intent)?.note ? <p className={`md-app-block__note is-${actionState(block.action.intent).tone}`} role="status">{actionState(block.action.intent).note}</p> : null}
      {sessionState?.notice ? <p className="md-app-block__notice" role="status">{sessionState.notice}</p> : null}
      {unverified}{view}
    </section>
  }
  if (block.pull) {
    // The title already opens the app, so a PR row has no separate Open link
    // or details toggle: its one action is the only inline view.
    const label = block.pull.number ? `Pull request ${block.pull.repo}#${block.pull.number}` : `Proposed pull request for ${block.pull.repo}`
    return <section ref={rootRef} className={`md-app-block md-app-block--pull${isSession ? ' md-app-block--compact' : ''}`} aria-label={label}>
      <PullSnapshot block={block} pull={block.pull} href={href} open={open} action={action} session={actionState(block.action?.intent)} appName={appName} confirming={confirmingAny} compact={isSession} />
      {sessionState?.notice ? <p className="md-app-block__notice" role="status">{sessionState.notice}</p> : null}
      {unverified}{view}
    </section>
  }
  return <section ref={rootRef} className={`md-app-block${canExpand ? '' : ' md-app-block--link'}`} aria-label={block.title}>
    <header><strong>{block.title}</strong><span className="md-app-block__header-actions">
      {actionState(block.action?.intent)?.links.length ? <span className="md-app-block__links">{actionState(block.action.intent).links.map(link =>
        <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer">{link.label}</a>)}</span> : null}
      {action}<a href={href} onClick={open}>Open{app ? ` in ${app.name}` : ''}<ChevronRight width={16} height={16} aria-hidden="true" /></a></span></header>
    {block.facts.length > 0 && <dl>{block.facts.map((fact, i) => <div key={i}><dt>{fact.label}</dt><dd>{fact.href ? <a href={fact.href} target="_blank" rel="noopener noreferrer">{fact.value}</a> : fact.value}</dd></div>)}</dl>}
    {toggle}{unavailable}{sessionState?.notice ? <p className="md-app-block__notice" role="status">{sessionState.notice}</p> : null}{unverified}{view}
  </section>
}
