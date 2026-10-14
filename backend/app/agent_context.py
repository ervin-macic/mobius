"""What Möbius adds to a chat agent's context: the parts, and their sizes.

Owner observability only. The "What the agent knows" inspector shows these
texts and the composer's ``/context`` panel shows how much of the context
window each one takes. Both read the parts through ``assemble_agent_context``,
which uses the same builders ``run_chat`` uses, without running a turn, so the
two views can never describe different things.

Sizes are estimates (about four characters per token, the same rule the
connector catalog uses). Providers report only one total for a session, so the
panel measures the parts Möbius adds here and shows the rest of the provider's
total as the conversation and the provider's own instructions and tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app import models


@dataclass(frozen=True)
class AgentContextParts:
  """The texts Möbius assembles for a chat's agent, reconstructed read-only."""

  system_prompt: str
  # "snapshot" once the first turn captured the prompt; a preview before that.
  system_prompt_source: str
  system_prompt_origin: str
  skills_list: str
  recent_chats: str | None
  recent_chat_entries: list = field(default_factory=list)
  app_context: str | None = None
  app_report: str | None = None
  compaction_brief: str | None = None


def assemble_agent_context(
  db: Session, chat: models.Chat, data_dir: str | Path,
) -> AgentContextParts:
  """Rebuild the parts the chat's agent receives, without starting a turn."""
  # Imported per call, like the route always did: app.chat is heavy and these
  # builders are its private turn helpers.
  from app import memory
  from app.chat import _build_available_skills_block, _read_skill_text
  from app.chat_context import (
    _build_app_context,
    _build_app_report_block,
    _chat_settings_dict,
    _custom_system_prompt,
    _latest_compaction_brief,
    recent_chat_summary_order,
  )
  from app.providers import get_skill_origin
  from app.system_prompts import prompt_for_chat

  custom = _custom_system_prompt(_chat_settings_dict(chat))
  system_prompt = prompt_for_chat(
    chat,
    custom if custom else _read_skill_text(),
    db,
    persist=False,
  )
  app_context, _env = _build_app_context(db, chat.id, data_dir)
  recent_chat_block = memory.build_memory_block(
    data_dir,
    ordered_chat_ids=recent_chat_summary_order(db),
  )
  return AgentContextParts(
    system_prompt=system_prompt,
    system_prompt_source=(
      "snapshot" if chat.system_prompt_snapshot_id
      else ("custom_preview" if custom else "skill_preview")
    ),
    system_prompt_origin="custom" if custom else get_skill_origin(),
    skills_list=_build_available_skills_block(data_dir),
    recent_chats=recent_chat_block.text or None,
    recent_chat_entries=recent_chat_block.entries,
    app_context=app_context,
    app_report=_build_app_report_block(db, chat.id, data_dir),
    compaction_brief=_latest_compaction_brief(chat),
  )


def estimate_tokens(text: str | None) -> int:
  """About four characters per token, rounded up; 0 for an absent part."""
  return (len(text) + 3) // 4 if text else 0


# The /context panel's rows, in display order. Keys are the frontend's
# contract (ContextPanel labels them); a new part needs a label there too.
def context_part_sizes(parts: AgentContextParts) -> list[dict]:
  """Estimated tokens for each part Möbius adds, zero when a part is absent.

  The app report and the app context travel together in the first turn's
  message, so they are one row. When Codex lists skills natively instead of
  receiving Möbius's block, its list carries the same entries, so the block's
  size stands in for it. Provider tool definitions are not listed here: Möbius
  cannot see the provider's built-in tools, so they stay in the provider's
  share of the total rather than appearing as an invented number.
  """
  app = "\n\n".join(text for text in (parts.app_context, parts.app_report) if text)
  return [
    {"key": "instructions", "tokens": estimate_tokens(parts.system_prompt)},
    {"key": "skills", "tokens": estimate_tokens(parts.skills_list)},
    {"key": "recent_chats", "tokens": estimate_tokens(parts.recent_chats)},
    {"key": "app_context", "tokens": estimate_tokens(app)},
    {"key": "compaction", "tokens": estimate_tokens(parts.compaction_brief)},
  ]
