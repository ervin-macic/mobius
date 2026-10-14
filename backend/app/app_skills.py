"""Shared skill files that installed apps declare in their manifests.

An app's manifest ``skills`` names root-level ``<id>.md`` files or ``<id>/``
folders among its source files. After an install commits, they are copied
into ``/data/shared/skills``; an installer-owned sidecar records which app
owns each file, so one app never takes over another's skill and an owner's
edits are snapshotted before an update replaces them. Uninstall deactivates
them, recovery restores them, and the purge after the recovery window
releases them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app import data_git, fs_locks, models, settled
from app.config import get_settings
from app.manifest_contract import SKILL_MAX_BYTES as _CONTRACT_SKILL_MAX_BYTES
from app.manifest_contract import skill_member_paths
from app.storage_io import atomic_write

log = logging.getLogger("mobius.install")

# Shared skill files an app declares via manifest `skills`: a root-level
# `<id>.md` or a `<id>/` folder (SKILL.md + sibling markdown) whose
# `source_files` the post-commit sync phase copies into /data/shared/skills/.
# Records key by that relative path. Small caps — skills are prose, not data.
_SKILL_MAX_BYTES = _CONTRACT_SKILL_MAX_BYTES


# Installer-owned ownership/provenance sidecar inside the skills dir. A
# dotfile that is not `*.md`, so the skill loaders (the app-skills catalog
# app, the SDK skill-load observers) never list or read it.
_APP_SKILLS_SIDECAR = ".app-skills.json"


def _app_skill_id(rel: str) -> str:
  """The shared-skill id a recorded app skill path belongs to."""
  folder, sep, _ = rel.partition("/")
  return folder if sep else Path(rel).stem


def _is_app_skill_rel(rel: str) -> bool:
  """A record path the installer may touch: `<id>.md` or `<id>/<file>.md`."""
  parts = rel.split("/")
  if len(parts) == 1:
    return rel.endswith(".md") and Path(rel).name == rel and rel[0] != "."
  return (
    len(parts) == 2
    and all(part and not part.startswith(".") for part in parts)
    and parts[1].endswith(".md")
  )


def _prune_empty_skill_folder(root: Path, rel: str) -> None:
  """Drop a folder skill's directory once its last member has moved away."""
  if "/" in rel:
    with contextlib.suppress(OSError):
      (root / rel.partition("/")[0]).rmdir()


async def _sync_app_skills(
  db: Session,
  app: "models.App",
  manifest: dict,
  warnings: list[str],
) -> None:
  """Materializes manifest-declared skill files into /data/shared/skills.

  Post-commit best-effort phase (same contract as cron): the app row is
  already durable, so every failure appends a warning instead of raising.
  Bytes come from the app's FINAL on-disk source tree, which is uniform
  across the synthetic, clone, and post-merge update paths (validation
  guarantees skills ⊆ root source_files, so the tree carries them).

  The never-lose-work contract: a present skill file whose bytes differ
  from what this installer last recorded (agent edits) is git-snapshotted
  into the /data repo BEFORE being
  overwritten, and left untouched when the snapshot cannot be guaranteed.
  Ownership rides the installer-owned sidecar so one app can never
  silently take over another live app's skill file.
  """
  skills = skill_member_paths(manifest)
  data_dir = Path(get_settings().data_dir)
  skills_dir = data_dir / "shared" / "skills"
  source_dir = Path(app.source_dir)
  version = str(manifest.get("version", "unknown"))
  async with fs_locks.shared_skills_lock():
    from app import skills as skills_mod

    # Resolve any crash-interrupted /api/skills install FIRST, so a pending
    # directory skill is either a fully-owned collision below or gone — the
    # same reconciliation the direct install/uninstall paths run under this
    # lock. Then read the installed-skills sidecar so this writer honors the
    # SAME basename-collision policy as a direct install: a flat `foo.md` must
    # never coexist with an installed directory skill `foo/` under one id.
    skills_mod.reconcile_installed(skills_dir)
    installed_records = skills_mod._read_sidecar(
      skills_dir / skills_mod.INSTALLED_SKILLS_SIDECAR
    )
    # Platform seeds have a recorded owner too. An app may continue managing a
    # basename it already owns, but may not silently adopt a platform skill
    # merely because both use the historical flat-file shape.
    seed_sidecar = skills_dir / skills_mod.SEED_SKILLS_SIDECAR
    if seed_sidecar.is_symlink():
      warnings.append("skills: platform ownership unreadable — sync skipped")
      return
    if seed_sidecar.exists():
      try:
        seed_records = json.loads(seed_sidecar.read_text(encoding="utf-8"))
      except (OSError, ValueError):
        seed_records = None
      if (
        not isinstance(seed_records, dict)
        or any(not isinstance(name, str) or not isinstance(rec, dict)
               for name, rec in seed_records.items())
      ):
        warnings.append("skills: platform ownership unreadable — sync skipped")
        return
    else:
      seed_records = {}
    seed_owned = {
      name for name, rec in seed_records.items()
      if isinstance(rec, dict) and rec.get("status") != "retired"
    } | {
      f"{name}.md" for name in skills_mod._seed_names()
    }
    sidecar_path = skills_dir / _APP_SKILLS_SIDECAR
    records: dict = {}
    if sidecar_path.exists():
      try:
        loaded = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
          records = loaded
      except (OSError, ValueError):
        # A corrupt sidecar downgrades every present file to the
        # modified-or-unrecorded case — snapshot-then-overwrite — which is
        # the safe direction: work is preserved, ownership is re-earned.
        warnings.append("skills: ownership sidecar unreadable — rebuilding")
    desired = set(skills)
    # An update that drops a previously-owned skill must deactivate it just as
    # surely as an uninstall. Preserve the exact bytes in a retired archive,
    # then release the basename for another app.
    for rel, rec in list(records.items()):
      if not isinstance(rec, dict) or rec.get("app_id") != app.id:
        continue
      if rel in desired:
        continue
      target = skills_dir / rel
      retired = (
        skills_dir / ".inactive" / str(app.id) / "retired" / rel
      )
      inactive = skills_dir / ".inactive" / str(app.id) / rel
      source = target if target.is_file() else inactive
      if source.is_file():
        retired.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, retired)
      _prune_empty_skill_folder(skills_dir, rel)
      records.pop(rel, None)
      warnings.append(f"skill {rel}: retired by update")
    # Ownership and shape collisions are decided per skill id, so a folder
    # skill lands or is skipped as a whole rather than file by file.
    ours = {
      key for key, rec in records.items()
      if isinstance(rec, dict) and rec.get("app_id") == app.id
    }
    refused_ids: dict[str, str] = {}
    for skill_id in dict.fromkeys(_app_skill_id(rel) for rel in skills):
      is_folder = f"{skill_id}.md" not in skills
      flat, folder = skills_dir / f"{skill_id}.md", skills_dir / skill_id
      owners = {
        rec.get("app_id") for key, rec in records.items()
        if isinstance(rec, dict) and _app_skill_id(key) == skill_id
      } - {app.id, None}
      live_owner = None
      for owner_id in owners:
        live_owner = db.query(models.App).filter(models.App.id == owner_id).first()
        if live_owner is not None:
          break
      if f"{skill_id}.md" in seed_owned and f"{skill_id}.md" not in ours:
        refused_ids[skill_id] = "owned by the platform"
      elif live_owner is not None:
        # A hard-purged recorded owner no longer reserves the id.
        refused_ids[skill_id] = f"owned by app {live_owner.slug}"
      elif isinstance(installed_records.get(skill_id), dict) or (
        not is_folder and folder.is_dir()
      ):
        # Cross-SHAPE collision: an install-provenance skill may hold the same
        # logical id in the other on-disk shape. Refuse a second skill under
        # one id; the owner resolves it by removing one deliberately.
        refused_ids[skill_id] = (
          f"an installed skill already holds id {skill_id!r} "
          "(directory shape)"
        )
      elif is_folder and (
        flat.exists()
        # A folder is someone else's when it holds none of this app's recorded
        # members and a file this app does not declare. A folder of only our
        # declared files (e.g. after a sidecar rebuild) re-earns ownership
        # through the snapshot-then-overwrite path, like a flat skill.
        or (folder.is_dir()
            and not any(key.startswith(f"{skill_id}/") for key in ours)
            and any(f"{skill_id}/{child.name}" not in skills
                    for child in folder.iterdir()))
      ):
        refused_ids[skill_id] = (
          f"id {skill_id!r} is already held by another file or folder"
        )
    for rel in skills:
      if _app_skill_id(rel) in refused_ids:
        warnings.append(f"skill {rel}: {refused_ids[_app_skill_id(rel)]} — skipped")
        continue
      try:
        content = (source_dir / rel).read_bytes()
      except OSError:
        # Validation checked the manifest's declaration, not the repo's
        # contents — a clone whose tree lacks the file lands here.
        warnings.append(f"skill {rel}: missing from installed source tree")
        continue
      if len(content) > _SKILL_MAX_BYTES:
        warnings.append(
          f"skill {rel}: exceeds {_SKILL_MAX_BYTES} bytes — skipped"
        )
        continue
      rec = records.get(rel)
      owner_id = rec.get("app_id") if isinstance(rec, dict) else None
      if (
        isinstance(rec, dict)
        and owner_id == app.id
        and rec.get("active", True) is False
      ):
        inactive = skills_dir / ".inactive" / str(app.id) / rel
        if inactive.is_file():
          retired = (
            skills_dir / ".inactive" / str(app.id) / "retired" / rel
          )
          retired.parent.mkdir(parents=True, exist_ok=True)
          os.replace(inactive, retired)
      target = skills_dir / rel
      recorded_sha = rec.get("sha256") if isinstance(rec, dict) else None
      if target.exists():
        current_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        if current_sha != recorded_sha:
          # Modified since last recorded, or never recorded (an agent
          # edit): snapshot the current
          # bytes before replacing them — never trade edits for an update.
          if (data_dir / ".git").is_dir():
            try:
              ok, detail = await settled.run_settled(
                data_git.snapshot_path,
                data_dir,
                f"shared/skills/{rel}",
                f"pre-install snapshot of {rel} (app {app.slug} v{version})",
              )
            except Exception as exc:
              ok, detail = False, repr(exc)
            if not ok:
              warnings.append(
                f"skill {rel}: left unchanged (snapshot failed: {detail}); "
                "will retry next update"
              )
              continue
            warnings.append(f"skill {rel}: snapshotted then updated")
          else:
            warnings.append(
              f"skill {rel}: updated, no /data repo for snapshot"
            )
      target.parent.mkdir(parents=True, exist_ok=True)
      atomic_write(target, content)
      # 0o664 mirrors init_skills' boot convention — group-writable so the
      # agent can edit the skill no matter which uid materialized it.
      os.chmod(target, 0o664)
      records[rel] = {
        "app_id": app.id,
        "slug": app.slug,
        "manifest_url": app.manifest_url,
        "sha256": hashlib.sha256(content).hexdigest(),
        "installed_at": datetime.now(UTC).isoformat(),
        "active": True,
      }
      # Persist provenance immediately after each file lands: a crash
      # between files must never leave an installed skill without its
      # ownership record (the next install would treat it as agent-modified
      # and snapshot-then-overwrite — safe, but noisy). Installs are rare
      # and the JSON is tiny, so a per-file write costs nothing.
      atomic_write(
        sidecar_path,
        json.dumps(records, indent=2, sort_keys=True) + "\n",
      )
    # Persist the final reconciliation too: a non-empty desired set can still
    # skip every file, and removals above must not be retried forever. Keep the
    # per-file writes for crash safety between successful materializations.
    skills_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(
      sidecar_path,
      json.dumps(records, indent=2, sort_keys=True) + "\n",
    )
    # The skill set changed (or may have) — refresh the generated tier-1 index
    # INSIDE the lock. Regenerating after release let a concurrent direct
    # install/uninstall's newer index be overwritten by this writer's stale
    # snapshot. Best-effort like everything else in this post-commit phase.
    _regenerate_skills_index(skills_dir)


def _read_app_skill_records(skills_dir: Path) -> tuple[Path, dict]:
  sidecar = skills_dir / _APP_SKILLS_SIDECAR
  try:
    value = json.loads(sidecar.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    value = {}
  return sidecar, value if isinstance(value, dict) else {}


async def deactivate_app_skills(app_id: int) -> list[str]:
  """Remove one app's skills from discovery while preserving exact bytes."""
  warnings: list[str] = []
  skills_dir = Path(get_settings().data_dir) / "shared" / "skills"
  async with fs_locks.shared_skills_lock():
    sidecar, records = _read_app_skill_records(skills_dir)
    changed = False
    for rel, rec in records.items():
      if not isinstance(rec, dict) or rec.get("app_id") != app_id:
        continue
      if not _is_app_skill_rel(rel):
        warnings.append(f"skill record {rel!r}: invalid path")
        continue
      target = skills_dir / rel
      inactive = skills_dir / ".inactive" / str(app_id) / rel
      try:
        if target.is_file():
          inactive.parent.mkdir(parents=True, exist_ok=True)
          os.replace(target, inactive)
          _prune_empty_skill_folder(skills_dir, rel)
        if inactive.is_file():
          # Keep ``sha256`` as the last app-supplied baseline. The inactive
          # digest records the exact preserved bytes separately; otherwise an
          # owner-edited skill would become its own new baseline and a later
          # app update could overwrite it without taking the normal snapshot.
          rec["inactive_sha256"] = hashlib.sha256(
            inactive.read_bytes()
          ).hexdigest()
          rec["active"] = False
          rec["inactive_path"] = str(inactive.relative_to(skills_dir))
          rec["deactivated_at"] = datetime.now(UTC).isoformat()
          changed = True
      except OSError as exc:
        warnings.append(f"skill {rel}: could not deactivate ({exc})")
    if changed:
      atomic_write(
        sidecar,
        json.dumps(records, indent=2, sort_keys=True) + "\n",
      )
    # Inside the lock: a stale post-release regen could clobber a concurrent
    # mutation's newer index.
    _regenerate_skills_index(skills_dir)
  return warnings


async def restore_app_skills(app_id: int) -> list[str]:
  """Restore exact tombstoned skill bytes and their discovery records."""
  warnings: list[str] = []
  skills_dir = Path(get_settings().data_dir) / "shared" / "skills"
  async with fs_locks.shared_skills_lock():
    sidecar, records = _read_app_skill_records(skills_dir)
    changed = False
    for rel, rec in records.items():
      if (
        not isinstance(rec, dict)
        or rec.get("app_id") != app_id
        or rec.get("active", True) is not False
      ):
        continue
      if not _is_app_skill_rel(rel):
        warnings.append(f"skill record {rel!r}: invalid path")
        continue
      inactive = skills_dir / ".inactive" / str(app_id) / rel
      target = skills_dir / rel
      if not inactive.is_file():
        warnings.append(f"skill {rel}: preserved bytes are missing")
        continue
      try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
          if target.read_bytes() != inactive.read_bytes():
            conflict = (
              skills_dir / ".inactive" / str(app_id) / "conflicts" / rel
            )
            conflict.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, conflict)
            warnings.append(
              f"skill {rel}: preserved a file created while app was inactive"
            )
          else:
            target.unlink()
        os.replace(inactive, target)
        rec["active"] = True
        rec.pop("inactive_path", None)
        rec.pop("inactive_sha256", None)
        rec["restored_at"] = datetime.now(UTC).isoformat()
        changed = True
      except OSError as exc:
        warnings.append(f"skill {rel}: could not restore ({exc})")
    if changed:
      atomic_write(
        sidecar,
        json.dumps(records, indent=2, sort_keys=True) + "\n",
      )
    # Inside the lock: a stale post-release regen could clobber a concurrent
    # mutation's newer index.
    _regenerate_skills_index(skills_dir)
  return warnings


async def purge_app_skills(app_id: int) -> None:
  """Release records and inactive archives after the recovery TTL expires."""
  skills_dir = Path(get_settings().data_dir) / "shared" / "skills"
  async with fs_locks.shared_skills_lock():
    sidecar, records = _read_app_skill_records(skills_dir)
    for rel, rec in list(records.items()):
      if isinstance(rec, dict) and rec.get("app_id") == app_id:
        target = skills_dir / rel
        if rec.get("active", True) and target.is_file():
          try:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest == rec.get("sha256"):
              target.unlink()
              _prune_empty_skill_folder(skills_dir, rel)
          except OSError:
            pass
        records.pop(rel, None)
    shutil.rmtree(
      skills_dir / ".inactive" / str(app_id), ignore_errors=True,
    )
    atomic_write(
      sidecar,
      json.dumps(records, indent=2, sort_keys=True) + "\n",
    )
    # Inside the lock: a stale post-release regen could clobber a concurrent
    # mutation's newer index.
    _regenerate_skills_index(skills_dir)


def _regenerate_skills_index(skills_dir: Path) -> None:
  """Best-effort refresh of the generated skills-index.md after a change."""
  try:
    from app import skills as skills_mod

    skills_mod.write_index(skills_dir)
  except Exception:  # noqa: BLE001 - the index is a convenience surface
    log.warning("skills index regeneration failed", exc_info=True)
