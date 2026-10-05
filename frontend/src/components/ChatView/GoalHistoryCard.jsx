/* GoalHistoryCard shows a terminal Goal's outcome at its completion step. */

import { ChevronRight } from '@openai/apps-sdk-ui/components/Icon'
import GoalPlanDetails from './GoalPlanDetails.jsx'
import LifecycleIcon, { LifecycleOutcome } from './LifecycleIcon.jsx'
import { goalHistoryViewModel } from './goalHistory.js'

export default function GoalHistoryCard({ summary }) {
  const view = goalHistoryViewModel(summary)
  if (!view) return null
  const heading = <>
    <LifecycleIcon kind="goal" />
    <span className="chat__goal-history-copy">
      <span className="chat__goal-history-kicker">
        <LifecycleOutcome tone={view.completed ? 'completed' : summary.status === 'cancelled' ? 'stopped' : 'attention'} />{view.kicker}
      </span>
      <strong className={`chat__goal-history-objective${view.hasPlan ? ' chat__goal-history-objective--preview' : ''}`}>{view.objective}</strong>
      {!view.completed && view.reason && <span className="chat__goal-history-reason">{view.reason}</span>}
      {view.metadata && <span className="chat__goal-history-meta">{view.metadata}</span>}
    </span>
  </>

  return (
    <aside
      className={`chat__goal-history chat__goal-history--${view.completed ? 'completed' : summary.status === 'cancelled' ? 'cancelled' : 'failed'}`}
      aria-label={view.ariaLabel}
    >
      {view.hasPlan ? <details className="chat__goal-history-details">
        <summary className="chat__goal-history-summary" onPointerDown={event => event.preventDefault()}>
          {heading}
          <ChevronRight className="chat__panel-chevron" width={14} height={14} aria-hidden="true" />
        </summary>
        <GoalPlanDetails plan={summary.plan} />
      </details> : heading}
    </aside>
  )
}
