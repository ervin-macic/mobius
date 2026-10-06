import { lazy, Suspense, useCallback, useMemo, useState } from 'react'
import { ChevronDown, ChevronRight } from '@openai/apps-sdk-ui/components/Icon'
import { appQueries } from '../../../hooks/queries.js'
import { sharedBrowserShellHref } from '../../../lib/sharedBrowserWorkspace.js'
import './AppBlock.css'

const AppCanvas = lazy(() => import('../../AppCanvas/AppCanvas.jsx'))

const STATE_NAMES = { proposed: 'Not sent yet', open: 'Open', draft: 'Draft', merged: 'Merged', closed: 'Closed' }
// Purple is for links, so the state reads as a status pill in its own tone.
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
function PullSnapshot({ block, pull, href, open, action }) {
  const files = pull.files === null ? null : `${pull.files} ${pull.files === 1 ? 'file' : 'files'}`
  const ref = pull.number ? `${pull.repo}#${pull.number}` : pull.repo
  return <div className="md-app-pull">
    <div className="md-app-pull__headline">
      <a className="md-app-pull__title" href={href} onClick={open}>{block.title}</a>
      {pull.labels.map(label => <span key={label.name} className="md-app-pull__label" style={labelStyle(label.color)}>{label.name}</span>)}
    </div>
    <div className="md-app-pull__footer">
      <div className="md-app-pull__meta">
        <a className="md-app-pull__repo" href={pull.url || pull.repoUrl} target="_blank" rel="noopener noreferrer">{ref}</a>
        {pull.author ? <span>{pull.author}</span> : null}
        {files ? <span>{files}{pull.additions !== null ? <> <ins>+{pull.additions}</ins> <del>−{pull.deletions ?? 0}</del></> : null}</span> : null}
        <span className={`md-app-pull__badge is-${STATE_TONES[pull.state]}`}>{STATE_NAMES[pull.state]}</span>
        {pull.badges.map(badge => <span key={badge.label} className={`md-app-pull__badge is-${badge.tone}`}>{badge.label}</span>)}
      </div>
      {action}
    </div>
  </div>
}

/** Reuse the normal opaque app host, only when the reader opens this block. */
export default function AppBlock({ block, onInternalNav }) {
  const apps = appQueries.list.useQuery()
  const app = (apps.data || []).find(item => item.slug === block.app && !item.deleted_at)
  const canExpand = block.inline !== false
  // The open view's intent: the block's own, or its action's. null = closed.
  const [viewIntent, setViewIntent] = useState(null)
  const [delivered, setDelivered] = useState(false)
  const pending = useMemo(() => viewIntent ? { intent: viewIntent, nonce: crypto.randomUUID() } : null, [viewIntent])
  const href = sharedBrowserShellHref(block.href)
  const navigate = useCallback((target) => {
    const url = new URL(sharedBrowserShellHref(target), window.location.href)
    if (onInternalNav) onInternalNav(url)
    else window.location.assign(url.href)
  }, [onInternalNav])
  // A transcript is not a workspace navigation owner or a second chat-control
  // authority, so the embedded view may only open a conversation or an app.
  const hostRequest = useCallback((_, request) => {
    if (request.type === 'moebius:open-chat' && request.chatId) {
      navigate(`/shell/?${new URLSearchParams({ chat: request.chatId })}`)
    } else if (request.type === 'moebius:open-app' && request.appId) {
      navigate(`/shell/?${new URLSearchParams({ app: request.appId, ...(request.intent ? { intent: request.intent } : {}) })}`)
    } else {
      throw new Error('Open the app to use workspace controls. This transcript view can only open conversations and apps.')
    }
  }, [navigate])
  const openHref = target => event => {
    if (!onInternalNav || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault(); navigate(target)
  }
  const open = openHref(block.href)
  const show = intent => { setViewIntent(intent); setDelivered(false) }
  const actionButton = target => canExpand && app && target
    ? <button type="button" className="md-app-block__action" aria-pressed={viewIntent === target.intent}
      onClick={() => show(viewIntent === target.intent ? null : target.intent)}>{target.label}</button>
    : null
  const action = actionButton(block.action)
  const toggle = canExpand && app
    ? <button type="button" className="md-app-block__toggle" aria-expanded={viewIntent !== null}
      onClick={() => show(viewIntent === null ? block.intent : null)}>
      <ChevronDown width={16} height={16} aria-hidden="true" />{viewIntent !== null ? 'Hide details' : block.expandLabel || 'Show details here'}</button>
    : null
  const view = canExpand && app && pending && <div className="md-app-block__view" style={{ height: block.height }}>
    <Suspense fallback={<p role="status">Opening details…</p>}><AppCanvas key={viewIntent} appId={app.id} appName={app.name} appSlug={app.slug}
      version={app.updated_at || 0} offlineCapable={app.offline_capable} capabilityContract={app.capabilities}
      active={false} visible interactive pendingIntent={delivered ? null : pending}
      onIntentDelivered={() => setDelivered(true)} onHostRequest={hostRequest} /></Suspense>
  </div>
  const unavailable = canExpand && !app
    ? <p>{apps.isLoading ? 'Checking installed apps…' : `${block.app} is not available. The saved snapshot remains here.`}</p>
    : null
  if (block.items.length > 0) {
    return <section className="md-app-block md-app-block--batch" aria-label={block.title}>
      <header className="md-app-batch__head"><strong>{block.title}</strong><span>{block.items.length} {block.items.length === 1 ? 'item' : 'items'}</span></header>
      <ul className="md-app-batch__list">
        {block.items.map(item => <li key={item.intent}>
          {item.pull
            ? <PullSnapshot block={item} pull={item.pull} href={sharedBrowserShellHref(item.href)} open={openHref(item.href)} action={actionButton(item.action)} />
            : <a className="md-app-batch__title" href={sharedBrowserShellHref(item.href)} onClick={openHref(item.href)}>{item.title}</a>}
        </li>)}
      </ul>
      {action ? <footer className="md-app-batch__foot">{action}</footer> : null}
      {view}
    </section>
  }
  if (block.pull) {
    // The title already opens the app, so a PR row has no separate Open link
    // or details toggle: its one action is the only inline view.
    const label = block.pull.number ? `Pull request ${block.pull.repo}#${block.pull.number}` : `Proposed pull request for ${block.pull.repo}`
    return <section className="md-app-block md-app-block--pull" aria-label={label}>
      <PullSnapshot block={block} pull={block.pull} href={href} open={open} action={action} />
      {view}
    </section>
  }
  return <section className={`md-app-block${canExpand ? '' : ' md-app-block--link'}`} aria-label={block.title}>
    <header><strong>{block.title}</strong><span className="md-app-block__header-actions">{action}<a href={href} onClick={open}>Open{app ? ` in ${app.name}` : ''}<ChevronRight width={16} height={16} aria-hidden="true" /></a></span></header>
    {block.facts.length > 0 && <dl>{block.facts.map((fact, i) => <div key={i}><dt>{fact.label}</dt><dd>{fact.href ? <a href={fact.href} target="_blank" rel="noopener noreferrer">{fact.value}</a> : fact.value}</dd></div>)}</dl>}
    {toggle}{unavailable}{view}
  </section>
}
