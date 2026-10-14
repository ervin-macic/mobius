"""Hermetic lock and stale-snapshot contract for production deploy cutover."""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT = (Path(__file__).parents[2] / "scripts/deploy-prod.sh").read_text()


def _function(name: str) -> str:
  start = f"{name}() {{"
  return start + SCRIPT.split(start, 1)[1].split("\n}\n", 1)[0] + "\n}\n"


@pytest.fixture
def harness(tmp_path):
  state = tmp_path / "state"
  config = tmp_path / "config"
  state.mkdir(mode=0o700)
  config.mkdir(mode=0o700)
  lock = state / "replace.lock"
  lock.touch(mode=0o600)
  functions = _function("acquire_helper_replacement_fence") + _function(
    "revalidate_live_snapshot",
  )
  # Substitute only the two fixed host paths and the expected owner in this
  # disposable copy; production still requires root ownership and paths.
  functions = functions.replace("/var/lib/mobius-rebuild", str(state)).replace(
    "/etc/mobius-rebuild", str(config),
  ).replace('[ "$owner" != 0 ]', f'[ "$owner" != {os.geteuid()} ]')
  assert '[ "$owner" != 0 ]' in SCRIPT

  def run(*, target="prod", cid="cid0", image="image0", ref="ref0",
          config_hash="hash0"):
    script = f"""set -euo pipefail
fail() {{ echo "$*" >&2; }}
TARGET={target!r}; CONTAINER=mobius
RUNNING_CID=cid0; PREV_IMAGE=image0
RUNNING_IMAGE_REF=ref0; RUNNING_CONFIG_HASH=hash0
docker() {{
  case "$3" in
    '{{{{.Id}}}}') echo {cid!r} ;;
    '{{{{.Image}}}}') echo {image!r} ;;
    '{{{{.Config.Image}}}}') echo {ref!r} ;;
    *) echo {config_hash!r} ;;
  esac
}}
{functions}
acquire_helper_replacement_fence
revalidate_live_snapshot
echo CUTOVER_ALLOWED
"""
    return subprocess.run(["bash", "-c", script], text=True,
                          capture_output=True, check=False)

  return state, config, lock, run


def test_helper_lock_owner_blocks_deploy_before_live_mutation(harness):
  _, _, lock, run = harness
  with lock.open("r+") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    result = run()
  assert result.returncode != 0
  assert "helper owns" in result.stderr
  assert "CUTOVER_ALLOWED" not in result.stdout


def test_pending_helper_journal_blocks_even_after_lock_acquisition(harness):
  state, _, _, run = harness
  (state / "transaction.json").write_text("{}")
  result = run()
  assert result.returncode != 0
  assert "pending transaction" in result.stderr
  assert "CUTOVER_ALLOWED" not in result.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses discretionary file permissions")
def test_inaccessible_helper_lock_fails_closed(harness):
  _, _, lock, run = harness
  lock.chmod(0o000)
  denied = run()
  assert denied.returncode != 0
  assert "cannot open" in denied.stderr


def test_missing_helper_lock_fails_closed(harness):
  _, _, lock, run = harness
  lock.unlink()
  missing = run()
  assert missing.returncode != 0
  assert "private lock is unavailable" in missing.stderr


@pytest.mark.parametrize("changed", ["cid", "image", "ref", "config_hash"])
def test_stale_live_snapshot_aborts_before_cutover(harness, changed):
  _, _, _, run = harness
  result = run(**{changed: "changed"})
  assert result.returncode != 0
  assert "changed during build/preflight" in result.stderr
  assert "CUTOVER_ALLOWED" not in result.stdout


def test_matching_snapshot_under_helper_lock_allows_cutover(harness):
  _, _, _, run = harness
  result = run()
  assert result.returncode == 0, result.stderr
  assert "CUTOVER_ALLOWED" in result.stdout


def test_nonproduction_does_not_take_root_helper_lock(harness):
  _, _, lock, run = harness
  with lock.open("r+") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    result = run(target="test")
  assert result.returncode == 0, result.stderr


def test_early_privilege_gate_keeps_test_and_no_helper_compatible(tmp_path):
  function = _function("preflight_helper_deploy_privilege").replace(
    "/var/lib/mobius-rebuild", str(tmp_path / "state"),
  ).replace("/etc/mobius-rebuild", str(tmp_path / "config"))
  script = f"fail() {{ echo \"$*\" >&2; }}\n{function}\npreflight_helper_deploy_privilege\n"
  for target in ("prod", "test"):
    result = subprocess.run(["bash", "-c", f"TARGET={target}\n{script}"],
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr

  (tmp_path / "state").mkdir()
  result = subprocess.run(["bash", "-c", f"TARGET=test\n{script}"],
                          text=True, capture_output=True, check=False)
  assert result.returncode == 0, result.stderr


def test_fence_precedes_source_install_and_cutover_mutations():
  assert SCRIPT.index("preflight_helper_deploy_privilege\n\n# ── step 1") < SCRIPT.index(
    'step "[1/4] docker compose build"',
  )
  assert SCRIPT.index("  acquire_helper_replacement_fence\n  revalidate_live_snapshot\n"
                      ) < SCRIPT.index("--bundle \"$container_bundle\"")
  assert SCRIPT.index('step "[2/4] docker compose up -d') < SCRIPT.index(
    "  acquire_helper_replacement_fence\n  revalidate_live_snapshot\n",
    SCRIPT.index('step "[2/4] docker compose up -d'),
  ) < SCRIPT.index("if ! prepare_chat_cutover; then")


def test_real_root_private_helper_requires_privileged_deploy(tmp_path):
  """Exercise kernel permissions, not an owner-substituted lock fixture."""
  if os.geteuid() == 0:
    pytest.skip("requires a non-root test runner to prove unprivileged refusal")
  if shutil.which("sudo") is None or subprocess.run(
    ["sudo", "-n", "true"], capture_output=True, check=False,
  ).returncode != 0:
    pytest.skip("passwordless sudo is unavailable for disposable root fixture")

  state = tmp_path / "root-state"
  config = tmp_path / "root-config"
  subprocess.run([
    "sudo", "-n", "install", "-d", "-m", "0700", str(state), str(config),
  ], check=True)
  subprocess.run([
    "sudo", "-n", "install", "-m", "0600", "/dev/null",
    str(state / "replace.lock"),
  ], check=True)
  try:
    functions = (
      _function("preflight_helper_deploy_privilege")
      + _function("acquire_helper_replacement_fence")
    ).replace("/var/lib/mobius-rebuild", str(state)).replace(
      "/etc/mobius-rebuild", str(config),
    )
    script = f"""set -euo pipefail
fail() {{ echo "$*" >&2; }}
TARGET=prod
{functions}
preflight_helper_deploy_privilege
echo BUILD_REACHED
acquire_helper_replacement_fence
echo CUTOVER_ALLOWED
"""
    ordinary = subprocess.run(["bash", "-c", script], text=True,
                              capture_output=True, check=False)
    assert ordinary.returncode != 0
    assert "run this production deployment with sudo" in ordinary.stderr
    assert "BUILD_REACHED" not in ordinary.stdout
    assert "CUTOVER_ALLOWED" not in ordinary.stdout

    privileged = subprocess.run(["sudo", "-n", "bash", "-c", script],
                                text=True, capture_output=True, check=False)
    assert privileged.returncode == 0, privileged.stderr
    assert "CUTOVER_ALLOWED" in privileged.stdout
  finally:
    subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(state), str(config)],
                   check=True)


@pytest.mark.parametrize("target", ["prod", "test"])
def test_privileged_deploy_lock_ignores_retained_user_cache_and_never_truncates(
    tmp_path, target,
):
  """Real root open against a disposable root-owned target, not /run or Docker."""
  if os.geteuid() == 0 or shutil.which("sudo") is None or subprocess.run(
    ["sudo", "-n", "true"], capture_output=True, check=False,
  ).returncode != 0:
    pytest.skip("requires non-root runner with passwordless sudo")

  cache = tmp_path / "attacker-cache"
  cache.mkdir(mode=0o700)
  (cache / "mobius").mkdir(mode=0o700)
  root_parent = tmp_path / "root-parent"
  root_dir = root_parent / "mobius-deploy"
  victim = tmp_path / "root-owned-victim"
  subprocess.run(["sudo", "-n", "install", "-d", "-m", "0755", str(root_parent)],
                 check=True)
  subprocess.run(["sudo", "-n", "install", "-m", "0600", "/dev/null",
                  str(victim)], check=True)
  subprocess.run(["sudo", "-n", "bash", "-c",
                  'printf "DO_NOT_TRUNCATE" > "$1"', "fixture", str(victim)],
                 check=True)
  (cache / "mobius" / f"deploy-{target}.lock").symlink_to(victim)
  function = _function("initialize_deploy_lock").replace(
    "DEPLOY_LOCK_PARENT=/run", f"DEPLOY_LOCK_PARENT={root_parent}",
  )
  script = f'''set -euo pipefail
fail() {{ echo "$*" >&2; }}
TARGET={target}
{function}
initialize_deploy_lock
flock -n 9
echo LOCKED
'''
  try:
    result = subprocess.run(
      ["sudo", "-n", "env", f"HOME={cache}", f"XDG_CACHE_HOME={cache}",
       "bash", "-c", script],
      text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "LOCKED" in result.stdout
    assert subprocess.check_output(
      ["sudo", "-n", "cat", str(victim)], text=True,
    ) == "DO_NOT_TRUNCATE"
    assert subprocess.run(["sudo", "-n", "test", "-f",
                           str(root_dir / f"deploy-{target}.lock")],
                          check=False).returncode == 0
    assert (cache / "mobius" / f"deploy-{target}.lock").is_symlink()
    subprocess.run(["sudo", "-n", "rm", "--",
                    str(root_dir / f"deploy-{target}.lock")], check=True)
    subprocess.run(["sudo", "-n", "ln", "-s", str(victim),
                    str(root_dir / f"deploy-{target}.lock")], check=True)
    rejected = subprocess.run(
      ["sudo", "-n", "env", f"HOME={cache}", f"XDG_CACHE_HOME={cache}",
       "bash", "-c", script], text=True, capture_output=True, check=False,
    )
    assert rejected.returncode != 0
    assert "symlink" in rejected.stderr
    assert subprocess.check_output(
      ["sudo", "-n", "cat", str(victim)], text=True,
    ) == "DO_NOT_TRUNCATE"
  finally:
    subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(root_parent),
                    str(victim)], check=True)


def test_nonprivileged_deploy_lock_still_serializes(tmp_path):
  if os.geteuid() == 0:
    pytest.skip("ordinary-user lock contract")
  function = _function("initialize_deploy_lock")
  script = f'''set -euo pipefail
fail() {{ echo "$*" >&2; }}
TARGET=test
{function}
initialize_deploy_lock
flock -n 9
echo LOCKED
'''
  env = {**os.environ, "XDG_CACHE_HOME": str(tmp_path)}
  first = subprocess.run(["bash", "-c", script], env=env, text=True,
                         capture_output=True, check=False)
  assert first.returncode == 0, first.stderr
  lock = tmp_path / "mobius" / "deploy-test.lock"
  with lock.open("r+") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    blocked = subprocess.run(["bash", "-c", script], env=env, text=True,
                             capture_output=True, check=False)
  assert blocked.returncode != 0
  assert "LOCKED" not in blocked.stdout


# ── helper-managed bundled proxy reconcile ─────────────────────────────
# The controller replaces only `app`; the region below is the real cutover
# text from the script, driven with a fake Compose that models one rule:
# `up` may name only the bundled proxy with --no-deps, anything else touches
# the admission-managed app.
OLD_CID = "a" * 64
NEW_CID = "b" * 64
TARGET_ID = "sha256:" + "c" * 64
BUILT_SHA = "d" * 40


def _cutover_region() -> str:
  start = SCRIPT.index("\nTARGET_IMAGE=$(docker image inspect") + 1
  end = SCRIPT.index("run_deploy_canary\n", start) + len("run_deploy_canary\n")
  return SCRIPT[start:end]


@pytest.fixture
def cutover(tmp_path):
  state_dir = tmp_path / "rebuild-state"
  state_dir.mkdir(mode=0o700)
  events = tmp_path / "events"
  caddy_state = tmp_path / "caddy-state"
  region = _cutover_region().replace("/var/lib/mobius-rebuild", str(state_dir))
  functions = (_function("external_prod_caddy_running")
               + _function("reconcile_bundled_proxy")
               + _function("manual_controller_id"))

  def run(*, helper=1, edge=False, services="caddy app", up_rc=0,
          services_rc=0, controller_rc=0):
    caddy_state.write_text("image=caddy:old domain=old.example "
                           "gateway=https://old-services.example\n")
    compose_args = ("(-p mobius -f docker-compose.yml -f docker-compose.prod.yml)"
                    if edge else "(-p mobius)")
    script = f"""set -euo pipefail
EVENTS={str(events)!r}; CADDY_STATE={str(caddy_state)!r}
record() {{ printf '%s\\n' "$*" >> "$EVENTS"; }}
info() {{ :; }}; ok() {{ :; }}; warn() {{ :; }}; intent() {{ :; }}
fail() {{ printf 'FAIL %s\\n' "$*" >&2; }}
for name in attempt_rollback rearm_chat_cutover finalize_chat_cutover \\
    prepare_chat_cutover wait_for_cutover ensure_edge_network; do
  eval "$name() {{ record CALLED $name; }}"
done
compose_recreation_needed() {{ return 1; }}
cutover_supported() {{ return 0; }}
served_sha() {{ echo {BUILT_SHA}; }}
run_deploy_canary() {{ record "canary $CONTAINER"; }}
# Values the deploy resolved for this release; Compose renders Caddy from them.
export DOMAIN=new.example MOBIUS_SERVICE_GATEWAY_ORIGIN=https://new-services.example
RENDERED_CADDY_IMAGE=caddy:new
docker() {{
  record "docker $*"
  case "$*" in
    "image inspect -f {{{{.Id}}}} mobius:prod") echo {TARGET_ID} ;;
    *" config --format json") echo '{{}}' ;;
    *" config --services")
      [ {services_rc} = 0 ] || return 1
      printf '%s\\n' {services} ;;
    *" up "*|*" up")
      [ {up_rc} = 0 ] || return 1
      local argv="$*"
      if [ "${{argv##* up }}" != "-d --no-deps caddy" ]; then record APP_TOUCHED; return 0; fi
      printf 'image=%s domain=%s gateway=%s\\n' "$RENDERED_CADDY_IMAGE" \\
        "$DOMAIN" "$MOBIUS_SERVICE_GATEWAY_ORIGIN" > "$CADDY_STATE" ;;
  esac
}}
MANUAL_CONTROLLER=/fixture/mobius-manual-cutover.py
python3() {{
  if [ "${{3:-}}" = "$MANUAL_CONTROLLER" ]; then
    record "controller $4 --source-cid $6 --target-image ${{10}}"
    [ {controller_rc} = 0 ] || return 1
    printf '{{"container_id":"%s","state":"succeeded"}}' {NEW_CID}
    return 0
  fi
  command python3 "$@"
}}
TARGET=prod; HELPER_MANAGED={helper}; EDGE_TOPOLOGY={'edge' if edge else 'self-hosted'}
COMPOSE_ARGS={compose_args}; REPO_ROOT=/fixture/checkout
CONTAINER=mobius; RUNNING_CID={OLD_CID}; PREV_IMAGE=sha256:{'e' * 64}
IMAGE_TAG=mobius:prod; BUILT_THIS_RUN=1; BUILD_SHA={BUILT_SHA}
CUTOVER_WAIT_SECONDS=1; INTERNAL_BASE=http://localhost:8000
{functions}
{region}
record "final $CONTAINER"
"""
    result = subprocess.run(["bash", "-c", script], text=True,
                            capture_output=True, check=False)
    lines = events.read_text().splitlines() if events.exists() else []
    events.unlink(missing_ok=True)
    return result, lines, caddy_state.read_text()

  return run


def test_compose_model_requires_no_deps_to_spare_the_app():
  compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text()
  caddy = compose.split("\n  caddy:\n", 1)[1].split("\n  app:\n", 1)[0]
  # Caddy's routing environment changes with a deploy, and its dependency
  # on app would otherwise let Compose converge (or wait on) the app.
  assert "DOMAIN=${DOMAIN}" in caddy
  assert "MOBIUS_SERVICE_GATEWAY_ORIGIN=" in caddy
  assert "condition: service_healthy" in caddy


def test_helper_managed_self_hosted_applies_changed_proxy_without_app(cutover):
  result, events, caddy = cutover()
  assert result.returncode == 0, result.stderr
  assert events == [
    "docker image inspect -f {{.Id}} mobius:prod",
    "docker compose -p mobius config --format json",
    f"controller cutover --source-cid {OLD_CID} --target-image {TARGET_ID}",
    "docker compose -p mobius config --services",
    "docker compose -p mobius up -d --no-deps caddy",
    f"canary {NEW_CID}",
    f"final {NEW_CID}",
  ]
  assert caddy == ("image=caddy:new domain=new.example "
                   "gateway=https://new-services.example\n")


def test_proxy_reconcile_failure_is_honest_and_never_reaches_the_app(cutover):
  result, events, caddy = cutover(up_rc=1)
  assert result.returncode != 0
  assert "APP_TOUCHED" not in events
  assert not [line for line in events if line.startswith("CALLED ")]
  assert [line for line in events if line.startswith("controller ")] == [
    f"controller cutover --source-cid {OLD_CID} --target-image {TARGET_ID}",
  ]
  assert [line for line in events if " up " in line] == [
    "docker compose -p mobius up -d --no-deps caddy",
  ]
  assert not [line for line in events if line.startswith(("canary", "final"))]
  assert "domain=old.example" in caddy
  assert f"verified app replacement {NEW_CID[:12]} stays live" in result.stderr
  assert "docker compose -p mobius up -d --no-deps caddy" in result.stderr
  rebuild = f"docker exec -u mobius {NEW_CID} bash /app/scripts/rebuild_shell.sh"
  assert rebuild in result.stderr
  assert result.stderr.index(rebuild) < result.stderr.index("finally rerun with --check")
  assert "verifies only and does not rebuild the frontend" in result.stderr


def test_unrenderable_service_list_fails_before_any_compose_up(cutover):
  result, events, _ = cutover(services_rc=1)
  assert result.returncode != 0
  assert not [line for line in events if " up" in line]
  assert not [line for line in events if line.startswith(("CALLED ", "canary"))]
  assert "could not render the services" in result.stderr


def test_profile_disabled_bundled_proxy_is_not_activated(cutover):
  result, events, caddy = cutover(services="app")
  assert result.returncode == 0, result.stderr
  assert not [line for line in events if " up" in line]
  assert events[-2:] == [f"canary {NEW_CID}", f"final {NEW_CID}"]
  assert "domain=old.example" in caddy


def test_helper_managed_edge_topology_keeps_its_fragment_path(cutover):
  result, events, _ = cutover(edge=True)
  assert result.returncode == 0, result.stderr
  assert not [line for line in events if "config --services" in line or " up" in line]
  assert not [line for line in events if line.startswith("CALLED ")]
  assert events[-2:] == [f"canary {NEW_CID}", f"final {NEW_CID}"]
  # Edge routing is installed once, by step 2b after the canary.
  assert SCRIPT.index("run_deploy_canary\n") < SCRIPT.index(
    "if external_prod_caddy_running; then\n  step \"[2b/4] install edge fragment\"")


def test_controller_failure_never_falls_back_to_compose(cutover):
  result, events, _ = cutover(controller_rc=1)
  assert result.returncode != 0
  assert not [line for line in events if " up" in line or "--services" in line]
  assert not [line for line in events if line.startswith(("CALLED ", "canary"))]


def test_unmanaged_self_hosted_keeps_single_full_project_up(cutover):
  result, events, _ = cutover(helper=0)
  assert result.returncode == 0, result.stderr
  assert [line for line in events if " up" in line] == [
    "docker compose -p mobius up -d",
  ]
  assert not [line for line in events if "--services" in line or "controller" in line]
  assert "CALLED wait_for_cutover" in events
  assert events[-2:] == ["canary mobius", "final mobius"]


@pytest.mark.parametrize("target,helper,edge", [
  ("test", 1, "self-hosted"), ("prod", 0, "self-hosted"), ("prod", 1, "edge"),
])
def test_proxy_reconcile_is_scoped_to_helper_managed_self_hosted_prod(
    target, helper, edge,
):
  script = f"""set -euo pipefail
docker() {{ echo "docker $*"; }}
info() {{ :; }}; ok() {{ :; }}; intent() {{ :; }}; fail() {{ :; }}
TARGET={target}; HELPER_MANAGED={helper}; EDGE_TOPOLOGY={edge}
COMPOSE_ARGS=(-p mobius)
{_function("external_prod_caddy_running")}
{_function("reconcile_bundled_proxy")}
reconcile_bundled_proxy
"""
  result = subprocess.run(["bash", "-c", script], text=True,
                          capture_output=True, check=False)
  assert result.returncode == 0, result.stderr
  assert result.stdout == ""


@pytest.mark.parametrize('profile_enabled', [False, True])
def test_real_compose_service_selection_does_not_enable_disabled_proxy(tmp_path, profile_enabled):
  executable = os.environ.get('MOBIUS_TEST_COMPOSE')
  compose = [executable] if executable else ['docker', 'compose']
  if not shutil.which(compose[0]):
    pytest.skip('Compose parser unavailable; hosted proof exercises proxy reconciliation')
  path = tmp_path / 'compose.yml'
  path.write_text('''services:
  app:
    image: app-fixture
  caddy:
    image: caddy-fixture
    profiles: [disabled]
    depends_on: [app]
''')
  args = [*compose, '-p', 'proxy-profile-proof', '-f', str(path)]
  if profile_enabled:
    args += ['--profile', 'disabled']
  result = subprocess.run([*args, 'config', '--services'],
                          env={k: v for k, v in os.environ.items() if k != 'COMPOSE_PROFILES'},
                          check=True, capture_output=True, text=True, timeout=15)
  assert set(result.stdout.split()) == ({'app', 'caddy'} if profile_enabled else {'app'})


# ── disposable host-helper harness: controller quiescence ──────────────
# The real region from scripts/test-host-helper.sh, driven by a fake systemctl
# whose per-unit `show` output evolves call by call. No real systemctl, Docker
# or sleep is reachable: PATH is empty and every external is a recorded stub.
HOST_HARNESS = (Path(__file__).parents[2] / "scripts/test-host-helper.sh").read_text()
CONTROLLERS = ("mobius-rebuild.service", "mobius-rebuild-reconcile.service")


def _quiescence_region() -> str:
  start = HOST_HARNESS.index("# BEGIN CONTROLLER QUIESCENCE\n")
  end = HOST_HARNESS.index("# END CONTROLLER QUIESCENCE\n", start)
  return HOST_HARNESS[start:end]


def _unit(active, job="", load="loaded"):
  return f"LoadState={load}\nActiveState={active}\nJob={job}\n"


QUIET = _unit("inactive")


def _wait_quiescent(tmp_path, sequences, *, attempts=5):
  """sequences: unit -> successive `show` outputs (None = command failure).

  The last entry repeats once a sequence is exhausted."""
  events = tmp_path / "events"
  units = tmp_path / "units"
  empty = tmp_path / "empty-path"
  empty.mkdir(exist_ok=True)
  for unit in CONTROLLERS:
    folder = units / unit
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "count").write_text("0\n")
    entries = sequences.get(unit, [QUIET])
    for index, entry in enumerate(entries):
      if entry is None:
        (folder / f"{index}.fail").write_text("")
      else:
        (folder / str(index)).write_text(entry)
    (folder / "last").write_text(f"{len(entries) - 1}\n")
  script = f"""set -euo pipefail
EVENTS={str(events)!r}; UNITS={str(units)!r}
record() {{ printf '%s\\n' "$*" >> "$EVENTS"; }}
fail() {{ record FAIL; printf 'FAIL %s\\n' "$*" >&2; exit 1; }}
sleep() {{ record "sleep $*"; }}
docker() {{ record DOCKER_CALLED; return 99; }}
systemctl() {{
  record "systemctl $*"
  [ "$1" = show ] || return 97
  [ "$3 $4 $5 $6" = "--all --property=LoadState --property=ActiveState --property=Job" ] \\
    || return 98
  local dir="$UNITS/$2" n last line
  read -r n < "$dir/count"; read -r last < "$dir/last"
  printf '%s\\n' "$((n + 1))" > "$dir/count"
  (( n <= last )) || n=$last
  [ ! -e "$dir/$n.fail" ] || return 1
  while IFS= read -r line; do printf '%s\\n' "$line"; done < "$dir/$n"
}}
{_quiescence_region()}
wait_controllers_quiescent {attempts} 2
echo QUIESCENT
"""
  result = subprocess.run([shutil.which("bash"), "-c", script], text=True,
                          capture_output=True, check=False, timeout=30,
                          env={"PATH": str(empty)})
  lines = events.read_text().splitlines() if events.exists() else []
  return result, lines


def _shows(lines):
  return [line.split()[2] for line in lines if line.startswith("systemctl ")]


def test_quiescence_call_site_replaces_is_active_probe():
  code = "\n".join(line for line in _quiescence_region().splitlines()
                   if not line.lstrip().startswith("#"))
  assert "is-active" not in code
  assert not re.search(r"systemctl (stop|kill|restart|start|reset)", code)
  assert "CONTROLLER_UNITS=(mobius-rebuild.service mobius-rebuild-reconcile.service)" in code
  dispatch = HOST_HARNESS.index("  systemctl stop mobius-rebuild.path\n")
  queued = HOST_HARNESS.index('  nonce=$(queue "$PREVIOUS")\n', dispatch)
  between = HOST_HARNESS[dispatch:queued]
  assert "  wait_controllers_quiescent 60 2\n" in between
  assert "is-active" not in between
  settlement = HOST_HARNESS.index("# The worker reports before it exits; the launcher settles after it.")
  revision_check = HOST_HARNESS.index('[[ $(active_revision) == "$expected" ]]', settlement)
  assert "wait_controllers_quiescent 60 2\n" in HOST_HARNESS[settlement:revision_check]
  assert "is-active" not in HOST_HARNESS[settlement:revision_check]


def test_evolving_activating_deactivating_and_queued_jobs_are_awaited(tmp_path):
  result, lines = _wait_quiescent(tmp_path, {
    # is-active would call both of the first two states quiescent.
    CONTROLLERS[0]: [_unit("activating", "41"), _unit("activating"),
                     _unit("deactivating"), _unit("inactive")],
    # Inactive with a queued start job is not fenced; failed+no job is.
    CONTROLLERS[1]: [_unit("inactive", "57"), _unit("activating", "57"),
                     _unit("deactivating"), _unit("deactivating"),
                     _unit("failed", "0")],
  })
  assert result.returncode == 0, result.stderr
  assert "QUIESCENT" in result.stdout
  assert _shows(lines) == list(CONTROLLERS) * 5
  assert [line for line in lines if line.startswith("sleep")] == ["sleep 2"] * 4
  assert "DOCKER_CALLED" not in lines and "FAIL" not in lines


@pytest.mark.parametrize("unit", CONTROLLERS)
@pytest.mark.parametrize("state,job", [
  ("activating", ""), ("deactivating", ""), ("active", ""), ("reloading", ""),
  ("inactive", "88"), ("failed", "88"),
])
def test_either_controller_busy_or_queued_times_out_bounded(tmp_path, unit, state, job):
  result, lines = _wait_quiescent(tmp_path, {unit: [_unit(state, job)]}, attempts=3)
  assert result.returncode != 0
  assert "QUIESCENT" not in result.stdout
  assert f"after 3 checks: {unit}={state}/job:{job or 'none'}" in result.stderr
  assert _shows(lines) == list(CONTROLLERS) * 3
  assert [line for line in lines if line.startswith("sleep")] == ["sleep 2"] * 2
  assert all(line.startswith(("systemctl show ", "sleep ")) or line == "FAIL"
             for line in lines)


@pytest.mark.parametrize("rebuild,reconcile", [
  (_unit("inactive"), _unit("inactive")),
  (_unit("failed"), _unit("inactive", "0")),
  (_unit("inactive", "0"), _unit("failed")),
  # An older seed without the reconcile unit reports it not-found/inactive.
  (_unit("inactive"), _unit("inactive", load="not-found")),
])
def test_inactive_or_failed_without_job_is_accepted_at_once(tmp_path, rebuild, reconcile):
  result, lines = _wait_quiescent(tmp_path, {CONTROLLERS[0]: [rebuild],
                                             CONTROLLERS[1]: [reconcile]})
  assert result.returncode == 0, result.stderr
  assert _shows(lines) == list(CONTROLLERS)
  assert not [line for line in lines if line.startswith("sleep")]


@pytest.mark.parametrize("unit", CONTROLLERS)
@pytest.mark.parametrize("evidence,message", [
  (None, "systemctl show {unit} failed"),
  ("", "unusable LoadState ''"),
  ("LoadState=loaded\nActiveState=inactive\n", "unusable Job ''"),
  ("LoadState=loaded\nJob=\n", "unusable ActiveState ''"),
  (_unit("inactive", load="masked"), "unusable LoadState 'masked'"),
  (_unit("inactive", load="bad-setting"), "unusable LoadState 'bad-setting'"),
  (_unit("inactive", job="start"), "unusable Job 'start'"),
  (_unit("Inactive"), "unusable ActiveState 'Inactive'"),
  (_unit("inactive") + "ActiveState=inactive\n", "duplicate systemd property ActiveState"),
  (_unit("inactive") + "SubState=dead\n", "unexpected systemd property SubState"),
  (_unit("inactive") + "garbage\n", "unparseable systemd line: garbage"),
])
def test_unreadable_or_malformed_evidence_fails_closed_without_retry(
    tmp_path, unit, evidence, message,
):
  # Settle the other unit first so the failure is attributable to `unit`.
  result, lines = _wait_quiescent(tmp_path, {unit: [evidence, QUIET]})
  assert result.returncode != 0
  assert "QUIESCENT" not in result.stdout
  assert message.format(unit=unit) in result.stderr
  assert f"cannot prove {unit} is quiescent" in result.stderr
  assert not [line for line in lines if line.startswith("sleep")]
  assert _shows(lines) == list(CONTROLLERS[:CONTROLLERS.index(unit) + 1])
