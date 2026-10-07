"""Run the tests that own an installation's local backend edits across an update.

A platform update carries the owner's local edits onto a new release. Text-clean
merges and the import probe still miss semantic breaks: a local edit can keep
importing while calling something the release removed, and fail only when a
chat turn runs it. When local edits carry their own tests, those tests are the
cheapest evidence that the edits still work on the new release.

``select`` names the tests owned by the local delta between the release and the
candidate: changed test files plus ``tests/test_<module>.py`` for each changed
backend module. ``run`` executes them with the checkout's isolated
``scripts/wt-pytest.sh`` runner and reports failing test ids. The update runs
them on the served tree before activation and on the candidate after it, so only
tests that newly fail block the update; a test that was already failing never
holds an owner's updates hostage.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

RUNNER = "scripts/wt-pytest.sh"
TIMEOUT_SECONDS = 600
_SCRUBBED_ENV = (
  "PYTHONPATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
  "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE",
  "DATABASE_URL", "DATA_DIR", "SECRET_KEY",
)


@dataclass(frozen=True)
class LocalTestRun:
  """Failing test ids from one run, or why the run produced no evidence."""

  failed: frozenset[str] = frozenset()
  unavailable: str | None = None


def _changed_backend_paths(repo: Path, release: str, candidate: str) -> list[str]:
  proc = subprocess.run(
    ["git", "-C", str(repo), "diff", "--name-only", "--no-renames",
     "--diff-filter=d", release, candidate, "--", "backend/"],
    capture_output=True, text=True, check=False,
  )
  return [line for line in proc.stdout.splitlines() if line.endswith(".py")]


def _in_tree(repo: Path, rev: str, path: str) -> bool:
  return subprocess.run(
    ["git", "-C", str(repo), "cat-file", "-e", f"{rev}:{path}"],
    capture_output=True, check=False,
  ).returncode == 0


def select(repo: Path, release: str, candidate: str) -> list[str]:
  """Backend-relative test files owned by the local delta ``release..candidate``."""
  owned: set[str] = set()
  for path in _changed_backend_paths(repo, release, candidate):
    name = Path(path).name
    if path.startswith("backend/tests/") and name.startswith("test_"):
      owned.add(path)
    elif path.startswith("backend/app/") and name != "__init__.py":
      sibling = f"backend/tests/test_{Path(path).stem}.py"
      if _in_tree(repo, candidate, sibling):
        owned.add(sibling)
  return sorted(path.removeprefix("backend/") for path in owned)


def _failed_ids(report: Path) -> frozenset[str]:
  failed = set()
  for case in ET.parse(report).getroot().iter("testcase"):
    if case.find("failure") is not None or case.find("error") is not None:
      failed.add(f"{case.get('classname', '')}::{case.get('name', '')}")
  return frozenset(failed)


def run(repo: Path, tests: list[str], *, timeout: int = TIMEOUT_SECONDS) -> LocalTestRun:
  """Run the present subset of ``tests`` in the checkout as it is on disk now."""
  present = [test for test in tests if (repo / "backend" / test).is_file()]
  if not present:
    return LocalTestRun()
  if not (repo / RUNNER).is_file():
    return LocalTestRun(unavailable=f"{RUNNER} is missing")
  env = {key: value for key, value in os.environ.items() if key not in _SCRUBBED_ENV}
  with tempfile.TemporaryDirectory(prefix="mobius-local-tests-") as tmp:
    report = Path(tmp) / "report.xml"
    # The runner's own children (pytest) must die with it on timeout.
    proc = subprocess.Popen(
      ["bash", RUNNER, *present, "-q", f"--junitxml={report}"],
      cwd=str(repo), env=env, start_new_session=True,
      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
      output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
      os.killpg(proc.pid, signal.SIGKILL)
      proc.communicate()
      return LocalTestRun(unavailable=f"local tests timed out after {timeout}s")
    if not report.is_file():
      tail = (output or "").strip()[-500:]
      return LocalTestRun(unavailable=f"local tests did not run (exit {proc.returncode}): {tail}")
    try:
      return LocalTestRun(failed=_failed_ids(report))
    except ET.ParseError as exc:
      return LocalTestRun(unavailable=f"local test report was unreadable: {exc}")


def newly_failing(before: LocalTestRun, after: LocalTestRun) -> list[str] | None:
  """Tests the update broke; ``None`` when the comparison has no evidence.

  An unavailable baseline counts as "nothing was failing": the served tree is
  the one these edits were written against, so a failure only after the update
  is still the update's to explain. An unavailable candidate run after a
  working baseline is itself a regression the update introduced.
  """
  if after.unavailable is not None:
    if before.unavailable is not None:
      return None
    return [after.unavailable]
  return sorted(after.failed - before.failed)


def describe(broken: list[str]) -> str:
  shown = ", ".join(broken[:5])
  more = f" (+{len(broken) - 5} more)" if len(broken) > 5 else ""
  return (
    "Your local changes fail their own tests on this release: "
    f"{shown}{more}. The previous version keeps running; repair the local "
    "changes for this release, then update again."
  )
