"""Hermetic contract tests for the GitHub wait checker (no network or auth)."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
SHA = "a" * 40
OTHER = "b" * 40


def reply(runs=None, statuses=None, head=SHA):
    rollup = None
    if runs is not None or statuses is not None:
        rollup = {"contexts": {
            "checkRunCountsByState": [{"state": s, "count": n} for s, n in (runs or {}).items()],
            "statusContextCountsByState": [{"state": s, "count": n} for s, n in (statuses or {}).items()],
        }}
    return {"data": {"repository": {"pullRequest": {"commits": {"nodes": [
        {"commit": {"oid": head, "statusCheckRollup": rollup}}]}}}}}


def fixture_run(tmp_path, value, *, shell=False):
    fixture = tmp_path / "reply.json"
    fixture.write_text(value if isinstance(value, str) else json.dumps(value))
    gh = tmp_path / "gh"
    gh.write_text("""#!/usr/bin/env python3
import os, sys
assert sys.argv[1:3] == ['api', 'graphql'] and 'number=7' in sys.argv
text = open(os.environ['GH_FIXTURE']).read()
if text == 'FAIL':
    print('secret auth diagnostic', file=sys.stderr)
    sys.exit(1)
print(text)
""")
    gh.chmod(0o755)
    env = {**os.environ, "GH_FIXTURE": str(fixture), "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    command = ([str(SCRIPTS / "pr-checks.sh")] if shell else [sys.executable, str(SCRIPTS / "pr-checks.py"), "--json"])
    process = subprocess.run(command + ["owner/repo", "7", SHA[:12]], text=True, capture_output=True, env=env, timeout=10)
    return process if shell else (process, json.loads(process.stdout))


def test_pending_is_progress_and_shell_silent(tmp_path):
    value = reply({"SUCCESS": 140, "SKIPPED": 9, "IN_PROGRESS": 1}, {"EXPECTED": 0})
    process, observed = fixture_run(tmp_path, value)
    assert process.returncode == 0
    assert observed == {"state": "pending", "summary": "149 of 150 checks have finished; waiting for the rest.", "completed": 149, "total": 150}
    shell = fixture_run(tmp_path, value, shell=True)
    assert (shell.returncode, shell.stdout, shell.stderr) == (1, "", "")


def test_failed_checks_are_terminal_not_broken(tmp_path):
    value = reply({"FAILURE": 1, "SUCCESS": 1}, {"ERROR": 1})
    process, observed = fixture_run(tmp_path, value)
    assert process.returncode == 0
    assert observed == {"state": "met", "summary": "All 3 checks finished; 2 reported failure.", "completed": 3, "total": 3}
    assert fixture_run(tmp_path, value, shell=True).returncode == 0


def test_cancelled_skipped_and_neutral_finish_without_failure(tmp_path):
    _, observed = fixture_run(tmp_path, reply({"CANCELLED": 1, "SKIPPED": 1, "NEUTRAL": 1, "SUCCESS": 1}))
    assert observed == {"state": "met", "summary": "All 4 checks finished.", "completed": 4, "total": 4}


def test_expected_status_is_unfinished(tmp_path):
    _, observed = fixture_run(tmp_path, reply({"SUCCESS": 1}, {"EXPECTED": 1}))
    assert (observed["state"], observed["completed"], observed["total"]) == ("pending", 1, 2)


def test_replaced_head_is_diagnostic(tmp_path):
    value = reply({"SUCCESS": 1}, head=OTHER)
    _, observed = fixture_run(tmp_path, value)
    assert observed["state"] == "failed" and "not the published" in observed["summary"]
    shell = fixture_run(tmp_path, value, shell=True)
    assert shell.returncode == 2 and "not the published" in shell.stderr


@pytest.mark.parametrize("value", [
    "FAIL",
    "{bad",
    json.dumps({"data": {"repository": {"pullRequest": None}}}),
    json.dumps(reply({"SUCCESS": []})),
    json.dumps(reply({"SUCCESS": 1}, head="not-a-sha")),
])
def test_bad_evidence_is_safe_and_visible(tmp_path, value):
    process, observed = fixture_run(tmp_path, value)
    assert process.returncode == 0 and observed["state"] == "failed"
    assert "secret" not in observed["summary"] and len(observed["summary"]) <= 500
    shell = fixture_run(tmp_path, value, shell=True)
    assert shell.returncode == 2 and "secret" not in shell.stderr


def test_no_checks_remains_pending(tmp_path):
    _, observed = fixture_run(tmp_path, reply())
    assert (observed["state"], observed["completed"], observed["total"]) == ("pending", 0, 0)


def test_unknown_state_is_unfinished_not_met(tmp_path):
    _, observed = fixture_run(tmp_path, reply({"SUCCESS": 1, "SOME_NEW_STATE": 1}))
    assert (observed["state"], observed["completed"], observed["total"]) == ("pending", 1, 2)
