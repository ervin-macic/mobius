"""Exercise the Docker fixture's source-install/manual-host-handoff boundary."""

import json
import os
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/test-upgrade-path.sh"
SOURCE = SCRIPT.read_text()
HELPERS = SOURCE[SOURCE.index("body() {"):SOURCE.index('previous=$(image_sha')]
HANDOFF = {
  "state": "activation_needed",
  "activation": {
    "required_actions": ["host_maintenance"],
    "reasons": [{
      "code": "host_helper_migration",
      "paths": ["deployment/self-hosted-helper.required"],
    }],
  },
}


def _shell(fragment, payload, *, expected=True, status=200):
  env = {k: v for k, v in os.environ.items() if k not in {"BASH_ENV", "ENV"}}
  return subprocess.run(
    ["bash", "-eu", "-c", HELPERS + '\nfail() { echo "$*" >&2; exit 1; }\n'
     + 'reply="$1"; needs_helper="$2"\n' + fragment,
     "fixture", json.dumps(payload) + f"\n{status}", str(expected).lower()],
    env=env, text=True, capture_output=True, timeout=5,
  )


def _apply(payload, **kwargs):
  start = SOURCE.index('  applied_state=')
  end = SOURCE.index('  echo "5. the owner restarts', start)
  return _shell(SOURCE[start:end], payload, **kwargs)


def test_reviewed_helper_migration_installs_source_with_an_explicit_handoff():
  assert _apply(HANDOFF).returncode == 0


@pytest.mark.parametrize("state", ["updated", "up_to_date", "restart_needed", "rolled_back", "conflict"])
def test_expected_migration_cannot_silently_lose_its_handoff(state):
  assert _apply({**HANDOFF, "state": state}).returncode != 0


@pytest.mark.parametrize("activation", [
  {},
  {"required_actions": []},
  {"required_actions": ["host_maintenance"], "reasons": []},
  {**HANDOFF["activation"], "required_actions": ["host_maintenance", "proxy_reload"]},
  {**HANDOFF["activation"], "reasons": [{"code": "host_helper_migration", "paths": ["docker-compose.yml"]}]},
])
def test_unrelated_or_unproved_external_work_is_not_accepted(activation):
  assert _apply({**HANDOFF, "activation": activation}).returncode != 0


def test_unreviewed_helper_work_and_http_failures_still_fail():
  assert _apply(HANDOFF, expected=False).returncode != 0
  assert _apply(HANDOFF, status=500).returncode != 0


@pytest.mark.parametrize("state", ["updated", "up_to_date", "restart_needed"])
def test_ordinary_source_install_outcomes_remain_accepted(state):
  assert _apply({"state": state}, expected=False).returncode == 0


@pytest.mark.parametrize("retained", [False, True])
def test_healthy_restart_does_not_discharge_helper_maintenance(retained):
  start = SOURCE.rindex('if [ "$needs_helper" = true ]; then')
  end = SOURCE.index('echo "upgrade path: ${previous', start)
  payload = HANDOFF if retained else {"state": "up_to_date", "activation": {"required_actions": []}}
  result = _shell('api() { printf "%s\\n" "$reply"; }\n' + SOURCE[start:end], payload)
  assert (result.returncode == 0) is retained
