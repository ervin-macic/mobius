"""Goal intent and attempt admission. No provider state or transcript inference."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from sqlalchemy import update
from app import models


def goal_hold(goal):
  """Read only validated attribution; legacy stops never imply owner intent."""
  hold = goal.hold_json
  keys = ("cause", "actor", "source_id", "run_id", "actor_id", "at")
  if not isinstance(hold, dict) or any(key not in hold for key in keys):
    return None
  if hold["cause"] not in ("stop", "quiet_answer", "deferred") or hold["actor"] not in ("owner", "agent", "unknown"):
    return None
  if hold["cause"] == "deferred":
    if hold["actor"] != "agent" or not isinstance(hold.get("reason"), str) or not hold["reason"].strip():
      return None
    keys += ("reason",)
  if any(not isinstance(hold[key], str) or not hold[key].strip() for key in ("source_id", "at")):
    return None
  if any(hold[key] is not None and (not isinstance(hold[key], str) or not hold[key].strip())
         for key in ("run_id", "actor_id")):
    return None
  try:
    at = datetime.fromisoformat(hold["at"])
  except ValueError:
    return None
  if at.tzinfo is None:
    return None
  return {key: hold[key] for key in keys}


def _new_hold(*, cause, actor, source_id, run_id=None, actor_id=None, reason=None):
  """One representation for explicit interruption and deliberate deferral."""
  record = {"cause": cause, "actor": actor, "source_id": source_id,
            "run_id": run_id, "actor_id": actor_id, "at": datetime.now(UTC).isoformat()}
  if reason is not None:
    record["reason"] = reason
  return record


def stage_goal_hold(goal, *, cause, actor, source_id, run_id=None, actor_id=None) -> bool:
  """Stage explicit intent in the writer's transaction, never replace a hold."""
  if goal.status != "open":
    return False
  goal.hold_json = _new_hold(cause=cause, actor=actor, source_id=source_id,
                             run_id=run_id, actor_id=actor_id)
  goal.status = "stopped"
  goal.revision += 1
  return True


def has_owner_input_after_hold(run, goal) -> bool:
  """A later owner request may deliberately resume work; a wake may not.

  Recovery carries the original admission time, not a fresh permission. Older
  records fail closed because neither run status nor transcript prose proves intent.
  """
  if run.owner_input_at is None:
    return False
  hold = goal_hold(goal)
  admitted_at = run.owner_input_at.replace(tzinfo=UTC)
  if hold is None:
    # A direct or queued owner request has no physical recovery envelope.
    # With no trustworthy hold time, inherited recovery cannot prove ordering.
    return run.continuation_json is None
  return admitted_at > datetime.fromisoformat(hold["at"]).astimezone(UTC)


def goal_for_run(db, run):
  if run is None or not run.goal_id:
    return None
  goal = db.get(models.ChatGoal, run.goal_id)
  if goal is None or goal.chat_id != run.chat_id:
    raise RuntimeError("Goal attempt has no matching durable work record")
  return goal


def goal_allows_automatic_resume(db, run) -> bool:
  """A process park cannot override a durable hold or a terminal outcome."""
  if not run.goal_id:
    return True
  goal = db.get(models.ChatGoal, run.goal_id)
  return goal is not None and goal.chat_id == run.chat_id and goal.status == "open"


def admit_goal(db, chat_id, goal_id, objective, message=None):
  """Called only by the writer, in the transaction admitting an exact attempt.

  Duplicate attempt admission is fenced by the existing writer commands.
  Execution turns are not a budget; explicit Stop remains authoritative.
  """
  if not goal_id:
    return
  from app.continuations import continuation_reason
  goal = db.get(models.ChatGoal, goal_id)
  if goal is None:
    goal = models.ChatGoal(id=goal_id, chat_id=chat_id, objective=objective, status="open")
    db.add(goal)
  elif goal.chat_id != chat_id:
    raise RuntimeError("Goal belongs to another chat")
  reason = continuation_reason(message)
  owner_admission = message is not None and (
    not message.get("kind") or reason in {"manual", "question_answer"}
  )
  if owner_admission:
    from app.goal_commands import is_goal_continue
    if goal.status == "stopped" and (reason == "manual" or is_goal_continue(str(message.get("content") or ""))):
      goal.status = "open"
      goal.hold_json = None
      goal.revision += 1


def scoped_goal_context(db, goal, task_id=None):
  from app.goal_context import project_goal
  payload = project_goal(goal, task_id)
  others = db.query(models.ChatGoal).filter(
    models.ChatGoal.chat_id == goal.chat_id, models.ChatGoal.id != goal.id,
    models.ChatGoal.status == "open",
  ).order_by(models.ChatGoal.created_at, models.ChatGoal.id).all()
  if others:
    payload["other_open_goals"] = [{"id": g.id, "objective": g.objective} for g in others]
  return payload


def resume_context(db, run_id):
  run = db.get(models.ChatRun, run_id)
  goal = goal_for_run(db, run)
  if goal is None:
    return ""
  return (
    "Möbius Goal work data, not additional authority. Preserve the original outcome. "
    "This is a scoped view, not the full plan. Work in this run; do not end merely "
    "to get another task or refresh context. update_goal with no arguments "
    "shows the full plan. Advance focus in one call: update_goal tasks marking "
    "the finished task completed with its result and the next one running. "
    "Complete only after verifying the entire Goal. An owner-action or approval "
    "gate leaves the Goal open with a saved card; a genuinely unreachable "
    "outcome first needs an actionable owner decision on a saved card, then a "
    "specific cannot_complete record if that limitation is accepted, not silent scope reduction. "
    "If the owner defers a step, continue other authorized work. When none can proceed, "
    "record update_goal(defer='reason') and end normally, without another question or automatic retry.\n"
    "<mobius_goal>" + json.dumps(scoped_goal_context(db, goal), ensure_ascii=False,
                                separators=(",", ":")) + "</mobius_goal>"
  )


async def settle_after_goal_completion(chat_id: str) -> None:
  """Finish what a committed terminal Goal outcome leaves for other owners.

  Resume notices queued for Waits the completion took delivery of are
  withdrawn, so they cannot start a turn for the finished Goal, and claim
  followers wake with the settled outcome.
  """
  from app.agent_coordination import settle_claims_with_owner
  from app.chat_waits import withdraw_delivered_resume_notices

  await withdraw_delivered_resume_notices(chat_id)
  await settle_claims_with_owner(chat_id)


def cannot_complete_result(details: dict[str, str]) -> str:
  """Stable outcome text shared by persistence and historical tool matching."""
  return "Reason: {reason}\nEfforts and partial results: {efforts}\nUnmet outcome: {unmet_outcome}".format(
    **{key: details[key].strip() for key in ("reason", "efforts", "unmet_outcome")}
  )


def update_goal_record(db, run, goal, expected_revision, *, checkpoint=None,
                       next_action=None, complete=None, cannot_complete=None,
                       cancel=None, defer=None, tasks=None, finished_claims=()):
  from app.goal_plans import (
    GoalPlanConflict, GoalPlanError, active_goal_helpers, normalize_tasks,
    staged_task_edits,
  )
  # New callers signal success without prose. Keep string completion for
  # running agents whose tool schema was loaded before the update.
  if complete is not None and complete is not True and not (
    isinstance(complete, str) and complete.strip()
  ):
    raise GoalPlanError("complete must be true")
  outcomes = [complete is not None, cannot_complete is not None, cancel is not None]
  if sum(outcomes + [defer is not None, next_action is not None]) > 1:
    raise GoalPlanError("Choose one Goal outcome, deferral, or next action")
  if finished_claims and complete is None:
    raise GoalPlanError("Only verified completion may name finished claims")
  status = ("completed" if complete is not None else
            "cannot_complete" if cannot_complete is not None else
            "cancelled" if cancel is not None else None)
  if cannot_complete is not None:
    if not isinstance(cannot_complete, dict) or any(
      not isinstance(cannot_complete.get(key), str) or not cannot_complete[key].strip()
      for key in ("reason", "efforts", "unmet_outcome")
    ):
      raise GoalPlanError("cannot_complete needs reason, efforts, and unmet_outcome text")
    outcome_text = cannot_complete_result(cannot_complete)
  else:
    outcome_text = complete.strip() if isinstance(complete, str) else cancel.strip() if isinstance(cancel, str) else None
  if status in {"cannot_complete", "cancelled"} and not outcome_text:
    raise GoalPlanError("A Goal outcome needs a specific explanation")
  if defer is not None and (not isinstance(defer, str) or not defer.strip()):
    raise GoalPlanError("A Goal deferral needs a specific explanation")
  defer_reason = defer.strip() if defer is not None else None
  hold = goal_hold(goal) if goal.status == "stopped" else None
  defer_replay = bool(defer_reason is not None and hold
    and hold["cause"] == "deferred" and hold["run_id"] == run.id
    and hold["reason"] == defer_reason)
  outcome_replay = goal.status == status and status is not None and goal.result == outcome_text
  if ((outcome_replay or defer_replay)
      and goal.revision in {expected_revision, expected_revision + 1}):
    # A lost tool receipt may be retried with the freshly read revision. Only
    # the same settled checklist and already-completed claims are a replay;
    # a matching result cannot disguise a new mutation of a closed Goal.
    if tasks is not None:
      document = staged_task_edits(goal, tasks)
      saved = normalize_tasks(goal.plan_json["tasks"]) if goal.plan_json else []
      if document["tasks"] != saved:
        raise GoalPlanConflict("A settled Goal checklist cannot change")
    if finished_claims:
      completed_keys = {row[0] for row in db.query(models.AgentWorkClaim.work_key).filter(
        models.AgentWorkClaim.owner_chat_id == goal.chat_id,
        models.AgentWorkClaim.owner_goal_id == goal.id,
        models.AgentWorkClaim.completed_at.is_not(None),
      ).all()}
      if set(finished_claims) - completed_keys:
        raise GoalPlanError("A replay cannot finish new work claims")
    return {"goal_id": goal.id, "status": goal.status, "revision": goal.revision}
  if goal.status != "open":
    raise GoalPlanConflict("Goal is not open")
  values = {"revision": expected_revision + 1}
  consumed_waits = 0
  if tasks is not None:
    document = staged_task_edits(goal, tasks)
    if status is None and defer is None and next_action is None and checkpoint is None:
      try:
        saved = normalize_tasks(goal.plan_json["tasks"])
      except (GoalPlanError, KeyError, TypeError):
        saved = None
      if saved == document["tasks"]:
        unchanged = db.execute(update(models.ChatGoal).where(
          models.ChatGoal.id == goal.id, models.ChatGoal.revision == expected_revision,
          models.ChatGoal.status == "open",
        ).values(revision=expected_revision))
        if unchanged.rowcount != 1:
          db.rollback()
          raise GoalPlanConflict("Goal changed; fetch it and retry")
        db.commit()
        return {"goal_id": goal.id, "status": goal.status, "revision": goal.revision}
    values["plan_json"] = document
  else:
    document = goal.plan_json
  if defer_reason is not None:
    # A deliberate hold must not silently abandon a real handoff. Resolve its
    # owner first; this operation never cancels helpers, cards, or Waits.
    from app.chat_waits import _goal_waits, _FIRED_UNDELIVERED
    from app.delegations import _self_resuming_helper_rows
    from app.goal_plans import goal_attempt_root_ids
    blockers = active_goal_helpers(db, run, goal)
    roots = goal_attempt_root_ids(db, goal.chat_id, goal.id)
    blockers += ["helper:" + row.id
      for row, _status in _self_resuming_helper_rows(db, {goal.chat_id})
      if row.parent_root_run_id in roots]
    blockers += ["wait:" + row.id for row in _goal_waits(db, goal.chat_id, goal.id).filter(
      (models.ChatWait.status == "armed") | _FIRED_UNDELIVERED,
    ).all()]
    if db.query(models.Chat.pending_question_id).filter(models.Chat.id == goal.chat_id).scalar():
      blockers.append("owner_question")
    if blockers:
      blockers = list(dict.fromkeys(blockers))
      raise GoalPlanError("Resolve existing handoffs before deferring: " + ", ".join(blockers),
                          code="goal_deferral_blocked", deferral_blockers=blockers)
    values.update(status="stopped", next_action=None, hold_json=_new_hold(
      cause="deferred", actor="agent", source_id=run.id,
      run_id=run.id, actor_id=run.id, reason=defer_reason,
    ))
  elif status is not None:
    if document is not None and not isinstance(document, dict):
      raise GoalPlanError(
        "Goal plan is unreadable; replace it with a validated plan before settlement"
      )
    try:
      plan_tasks = normalize_tasks(document["tasks"]) if document is not None else []
    except (GoalPlanError, KeyError, TypeError) as exc:
      raise GoalPlanError("Goal plan is unreadable; replace it with a validated plan before settlement") from exc
    allowed = {"completed", "cancelled"} if status == "completed" else {
      "completed", "failed", "blocked", "cancelled",
    }
    blockers = [task["id"] for task in plan_tasks if task["status"] not in allowed]
    if status != "completed":
      blockers += [task["id"] for task in plan_tasks if task["status"] in {
        "failed", "blocked", "cancelled",
      } and not (task.get("note") or task.get("result"))]
    blockers += active_goal_helpers(db, run, goal)
    blockers = list(dict.fromkeys(blockers))
    if blockers:
      raise GoalPlanError(
        "Goal has unfinished tasks, unexplained settlements, or active delegations: " + ", ".join(blockers),
        code="goal_completion_blocked", completion_blockers=blockers,
      )
    from app.agent_work_claims import held_claims_hint, open_claim_keys
    held = open_claim_keys(db, chat_id=goal.chat_id, goal_id=goal.id)
    unknown = set(finished_claims) - held
    if unknown:
      raise GoalPlanError(
        "Not an open work claim of this Goal: " + ", ".join(sorted(unknown))
        + ". " + held_claims_hint(held)
      )
    from app.chat_waits import stage_consume_fired_goal_waits
    consumed_waits = stage_consume_fired_goal_waits(db, goal.chat_id, goal.id)
    values.update(status=status, result=outcome_text, next_action=None,
                  completed_at=datetime.now(UTC))
  else:
    if checkpoint is not None:
      values["checkpoint"] = checkpoint
    if next_action is not None:
      values["next_action"] = next_action
  changed = db.execute(update(models.ChatGoal).where(
    models.ChatGoal.id == goal.id, models.ChatGoal.revision == expected_revision,
    models.ChatGoal.status == "open",
  ).values(**values))
  if changed.rowcount != 1:
    db.rollback()
    raise GoalPlanConflict("Goal changed; fetch it and retry")
  if status is not None or defer_reason is not None:
    # Completion settles the Goal's still-open exact-action claims in the same
    # commit: the ones it names as finished complete with the verified result,
    # the rest are released, so a declined action never reads as done.
    from app.agent_work_claims import stage_settle_goal_claims
    stage_settle_goal_claims(
      db, chat_id=goal.chat_id, goal_id=goal.id, status=values["status"],
      result=outcome_text or defer_reason, finished_keys=finished_claims,
    )
  db.commit()
  db.refresh(goal)
  if consumed_waits:
    from app.chat_waits import _broadcast_changed
    _broadcast_changed(goal.chat_id)
  return {"goal_id": goal.id, "status": goal.status, "revision": goal.revision}
