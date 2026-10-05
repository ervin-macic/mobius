#!/usr/bin/env bash
# Real self-update of the self-hosted replacement helper. An owner installed
# the helper from <previous>; <target>'s image must bring its own worker to
# that host by itself, through the installed launcher and systemd units,
# driven only by requests the app writes into /data.
#
#   sudo scripts/test-host-helper.sh <previous-sha> <target-sha>
#
# Set MOBIUS_RELEASE_REPLAY=1 to also prove automatic dependency restoration
# with a strictly newer worker. Set MOBIUS_TRANSCRIPT_PROOF=1 to also prove
# that per-message transcript storage survives the real worker's round trip:
# <target> converts and writes, <previous> serves every chat exactly and
# writes, and <target>, requested once more, re-converts without loss. No live host may be used for either mode.
# Both SHAs must have published official images. Run on a disposable systemd
# host with Docker Compose (a CI runner); it installs root-owned units there.
#
# 1. Deploy <previous> from its own checkout, as an owner does, and set up the
#    owner.
# 2. Install the helper once from that checkout.
# 3. The app requests <target>: the installed worker replaces the container
#    and offers <target>'s worker when its revision is higher.
# 4. The app requests <previous> again: the offered worker performs this real
#    replacement and becomes the active worker.

set -euo pipefail

PREVIOUS="${1:?previous sha}"
TARGET="${2:?target sha}"
# These values become Git refs and JSON request values below.
[[ $PREVIOUS =~ ^[0-9a-f]{40}$ && $TARGET =~ ^[0-9a-f]{40}$ && $PREVIOUS != "$TARGET" ]] \
  || { echo "two distinct full release SHAs are required" >&2; exit 2; }
REPLAY=${MOBIUS_RELEASE_REPLAY:-0}
TRANSCRIPTS=${MOBIUS_TRANSCRIPT_PROOF:-0}
[[ $REPLAY == 0 || $REPLAY == 1 ]] || { echo "MOBIUS_RELEASE_REPLAY must be 0 or 1" >&2; exit 2; }
IMAGE=ghcr.io/mobius-os/mobius
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
STATUS=/var/lib/mobius-rebuild/status.json
ENV_FILE=$(mktemp /tmp/mobius-host-helper.XXXXXX.env)
SEED=$(mktemp -d /tmp/mobius-seed.XXXXXX)/checkout
export COMPOSE_PROJECT_NAME=mobius

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

fail() {
  echo "host helper: $*" >&2
  systemctl --no-pager status mobius-rebuild.service mobius-rebuild.path >&2 || true
  journalctl --no-pager -u mobius-rebuild.service -n 80 >&2 || true
  cat "$STATUS" >&2 2>/dev/null || true
  docker logs mobius --tail 60 >&2 2>&1 || true
  exit 1
}

field() {  # <json-file> <python expression over d>
  python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); print(eval(sys.argv[2]))' "$1" "$2"
}

revision_of() {  # <sha>: inspect the requested release, not this harness's checkout
  git -C "$ROOT" show "$1:scripts/mobius-rebuild-host.py" \
    | sed -n 's/^WORKER_REVISION = \([0-9][0-9]*\)$/\1/p'
}

active_revision() {
  python3 -c 'import json; print(json.load(open("/var/lib/mobius-rebuild/workers.json"))["active"]["revision"])'
}

wait_status() {  # <nonce> <state>: wait until the root status names this request
  for _ in $(seq 1 240); do
    if [[ -f $STATUS ]] && [[ $(field "$STATUS" 'd.get("request_nonce")') == "$1" ]]; then
      local state; state=$(field "$STATUS" 'd.get("state")')
      [[ $state == "$2" ]] && return 0
      case "$state" in failed|rolled_back|needs_recovery|no_change) fail "request ended $state";; esac
    fi
    sleep 5
  done
  fail "request $1 did not reach $2 within 20 minutes"
}

queue() {  # <sha>: write the request exactly as the app does; prints its nonce
  local nonce; nonce=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
  docker exec -u mobius mobius sh -c "
    printf '%s' '{\"version\":2,\"expected_sha\":\"$1\",\"nonce\":\"$nonce\"}' \
      > /data/mobius-rebuild/inbox/.request.tmp &&
    mv /data/mobius-rebuild/inbox/.request.tmp /data/mobius-rebuild/inbox/request.json" \
    || fail "the app could not queue a request"
  echo "$nonce"
}

replaced_with() {  # <sha>: the container runs and serves exactly that release
  [[ $(docker inspect -f '{{.Image}}' mobius) == $(docker image inspect -f '{{.Id}}' "$IMAGE:sha-$1") ]] \
    || fail "the running container is not sha-$1"
  [[ $(docker exec mobius curl -fsS http://127.0.0.1:8000/api/version \
        | python3 -c 'import json, sys; print(json.load(sys.stdin).get("sha"))') == "$1" ]] \
    || fail "the container does not serve sha-$1"
}

docker info >/dev/null || fail "Docker is unavailable"
# This test uses production unit/container names; never attach it to existing data.
if docker container inspect mobius >/dev/null 2>&1 \
   || docker volume inspect mobius_app_data >/dev/null 2>&1; then
  echo "host helper: use a fresh disposable host, not an existing installation" >&2
  exit 2
fi
for path in /etc/mobius-rebuild /var/lib/mobius-rebuild \
  /usr/local/libexec/mobius-rebuild-host /etc/systemd/system/mobius-rebuild.service \
  /etc/systemd/system/mobius-rebuild.path /etc/systemd/system/mobius-rebuild-reconcile.service; do
  if [[ -e $path || -L $path ]]; then
    echo "host helper: existing helper path $path; use a fresh disposable host" >&2
    exit 2
  fi
done

echo "host helper: installed at sha-${PREVIOUS:0:12}, updating to sha-${TARGET:0:12}"
git -C "$ROOT" worktree add --detach -q "$SEED" "$PREVIOUS"
if [[ ! -f $SEED/scripts/mobius-rebuild-launcher.py ]]; then
  [[ $REPLAY == 0 ]] || fail "replay requires a previous release with the launcher"
  echo "host helper: sha-${PREVIOUS:0:12} predates the launcher; nothing to prove yet"
  exit 0
fi
seeded=$(revision_of "$PREVIOUS")
target_revision=$(revision_of "$TARGET")
[[ $seeded =~ ^[0-9]+$ && $target_revision =~ ^[0-9]+$ ]] || fail "missing worker revision"
if [[ $REPLAY == 1 ]]; then
  (( target_revision > seeded )) || fail "replay must exercise a newer worker"
  git -C "$ROOT" cat-file -e "$TARGET:backend/app/app_setup.py" \
    || fail "target predates dependency restoration"
  docker pull "$IMAGE:sha-$TARGET"
  # A fresh target must lack BOTH test dependencies; otherwise this is no proof.
  docker run --rm --network none --entrypoint sh "$IMAGE:sha-$TARGET" -c '
    ! command -v figlet && python3 -c "import importlib.util; assert importlib.util.find_spec(\"pyfiglet\") is None"
  ' || fail "test packages already exist in the target image"
fi
printf 'SECRET_KEY=host-helper-regression-key-0123456789abcdef\nDOMAIN=localhost\n' >"$ENV_FILE"
chmod 0600 "$ENV_FILE"


# Transcript proof helpers (MOBIUS_TRANSCRIPT_PROOF=1). The probe runs the
# serving release's own code; waits poll observable state only.
TPROOF=$(mktemp -d /tmp/mobius-transcript-proof.XXXXXX)
tprobe() {  # <command>
  docker cp "$ROOT/scripts/transcript_rollback_probe.py" mobius:/tmp/probe.py
  docker exec -u mobius -w /data/platform/backend -e PYTHONPATH=/data/platform/backend \
    mobius python3 /tmp/probe.py "$1"
}
tconverge() {
  until [[ $(tprobe pending) == '{"pending": 0}' ]]; do sleep 1; done
}
tsame() {  # <label> <expected.json> <actual.json>: the damaged chat may become its placeholder
  python3 - "$2" "$3" "$1" <<'PY' || fail "transcripts differ"
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:3])
a.pop("damaged", None); damaged = b.pop("damaged", None)
assert damaged is None or damaged["messages"][0].get("transcript_damage") is True, damaged
# Canonical text, so 1 / 1.0 / True, -0.0 / 0.0 and NaN are distinguished.
a, b = ({k: json.dumps(v, sort_keys=True) for k, v in d.items()} for d in (a, b))
assert a == b, f"{sys.argv[3]}: {sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))[:5]}"
PY
}

echo "1. the previous release runs as an owner deploys it"
cd "$SEED"
MOBIUS_IMAGE="$IMAGE:sha-$PREVIOUS" docker compose --env-file "$ENV_FILE" \
  up -d --no-build --no-deps app
for _ in $(seq 1 60); do
  [[ $(docker inspect -f '{{.State.Health.Status}}' mobius 2>/dev/null) == healthy ]] && break
  sleep 5
done
[[ $(docker inspect -f '{{.State.Health.Status}}' mobius) == healthy ]] \
  || fail "the previous release did not become healthy"
# The worker's chat drain authenticates with the owner's service token.
docker exec mobius curl -fsS -o /dev/null -X POST -H 'Content-Type: application/json' \
  -d '{"username":"owner","password":"host-helper-owner-password"}' \
  http://127.0.0.1:8000/api/auth/setup || fail "owner setup failed"
for _ in $(seq 1 30); do
  docker exec mobius test -s /data/service-token.txt && break
  sleep 2
done
docker exec mobius test -s /data/service-token.txt || fail "the instance has no service token"
if [[ $TRANSCRIPTS == 1 ]]; then
  tprobe seed >/dev/null
  tprobe dump >"$TPROOF/seeded.json"
fi

echo "2. the owner installs the helper once, from that checkout"
scripts/install-rebuild-helper.sh || fail "the installer failed"
[[ $(field "$STATUS" 'd.get("launcher_revision")') == 1 ]] \
  || fail "the installed helper is not the launcher"
[[ $(active_revision) == "$seeded" ]] || fail "the installed worker is not revision $seeded"

if [[ $REPLAY == 1 ]]; then
  echo "   prepare accepted declarations and the reviewed source update"
  docker exec -u mobius mobius mkdir -p /data/customizations
  docker cp "$ROOT/scripts/fixtures/release-replay/." mobius:/data/customizations/
  docker exec -u root mobius chown -R mobius:mobius /data/customizations
  # Install only in the disposable OLD container, never in its image. The new
  # container must restore them automatically from the persistent declaration.
  docker exec -u mobius mobius sudo -n apt-get update --error-on=any
  docker exec -u mobius mobius sudo -n apt-get install --yes --no-remove figlet
  docker exec -u mobius mobius sh /data/customizations/restore-python.sh apply
  docker exec -u mobius mobius sh /data/customizations/restore-python.sh check
  docker exec -u mobius mobius figlet replay >/dev/null
  docker exec -u mobius mobius git -C /data/platform fetch origin "$TARGET"
  docker exec -i -u mobius -w /data/platform/backend mobius python3 - "$TARGET" <<'SOURCE'
import sys
from app import platform_update as pu
preview = pu.platform_update_preview(target_sha=sys.argv[1])
assert not preview["conflict_paths"], preview["conflict_paths"]
plan = {key: preview[key] for key in ("plan_id", "current_sha", "target_sha", "image_digest")}
prepared = pu.prepare_reviewed_update(**plan)
assert isinstance(prepared, dict) and prepared["state"] == "prepared", prepared
# This historical pair is restart-loadable; the real external cutover swaps it.
# Do not silently bypass the bound-operation protocol for an image-required plan.
assert not prepared["requires_image"], "choose a restart-loadable historical source pair"
SOURCE
fi
before=$(docker inspect -f '{{.Id}}' mobius)
echo "3. the app requests the target release"
nonce=$(queue "$TARGET")
wait_status "$nonce" succeeded
replaced_with "$TARGET"
[[ $(docker inspect -f '{{.Id}}' mobius) != "$before" ]] || fail "container was not replaced"
if [[ $TRANSCRIPTS == 1 ]]; then
  tconverge
  tprobe dump >"$TPROOF/converted.json"
  tsame "the target converted every chat exactly" "$TPROOF/seeded.json" "$TPROOF/converted.json"
  [[ $(tprobe mirror-exact) == '{"differ": []}' ]] || fail "a converted chat's legacy bytes are not its rows"
  tprobe write-new >/dev/null
  [[ $(tprobe mirror-exact) == '{"differ": []}' ]] || fail "a mirror after target writes is not byte-exact"
  tprobe dump >"$TPROOF/target.json"
fi
if [[ $REPLAY == 1 ]]; then
  # Never call setup/rerun here: startup alone must restore the declarations.
  for _ in $(seq 1 120); do
    if docker exec mobius python3 -c '
import json
from pathlib import Path
p = Path("/data/setup-status.json")
s = json.loads(p.read_text()) if p.exists() else {}
keys = ("apt", "instance:restore-python.sh")
raise SystemExit(0 if all(s.get(k, {}).get("state") == "ready" for k in keys) else 1)
'; then break; fi
    sleep 5
  done
  docker exec -u mobius mobius sh /data/customizations/restore-python.sh check \
    || fail "Python dependency was not automatically restored"
  docker exec -u mobius mobius figlet replay >/dev/null || fail "apt dependency was not restored"
  docker exec -i mobius python3 - "$TARGET" <<'VERIFY'
import json, subprocess, sys, urllib.request
from pathlib import Path
with urllib.request.urlopen("http://127.0.0.1:8000/api/version") as response:
    version = json.load(response)
assert version["serving_source"] == "platform", version
subprocess.run(["git", "-c", "safe.directory=/data/platform", "-C", "/data/platform",
                "merge-base", "--is-ancestor", sys.argv[1], version["served_sha"]], check=True)
states = json.loads(Path("/data/setup-status.json").read_text())
assert all(states[k]["state"] == "ready" for k in ("apt", "instance:restore-python.sh")), states
assert not Path("/data/.platform-prepared-update.json").exists(), "source activation unfinished"
assert Path("/data/customizations/preserved.txt").read_text() == "release-replay fixture\n"
print("release replay: target source loaded; apt and Python dependencies automatically restored")
VERIFY
fi
adoption=$(field "$STATUS" 'd.get("worker_adoption") or ""')
echo "   worker adoption: $adoption"
if (( target_revision > seeded )); then
  [[ $adoption == offered* ]] || fail "revision $target_revision was not offered"
fi

echo "4. the app requests the previous release again"
nonce=$(queue "$PREVIOUS")
wait_status "$nonce" succeeded
replaced_with "$PREVIOUS"
expected=$(( target_revision > seeded ? target_revision : seeded ))
# The worker reports before it exits; the launcher settles after it.
for _ in $(seq 1 60); do
  if ! systemctl is-active --quiet mobius-rebuild.service \
     && [[ $(active_revision) == "$expected" ]]; then break; fi
  sleep 2
done
[[ $(active_revision) == "$expected" ]] \
  || fail "worker revision $expected is not active (active: $(active_revision))"
[[ $(field "$STATUS" 'd.get("worker_revision")') == "$expected" ]] \
  || fail "revision $expected did not perform the replacement"
if [[ $REPLAY == 1 ]]; then
  expected_hash=$(git -C "$ROOT" show "$TARGET:scripts/mobius-rebuild-host.py" | sha256sum | cut -d ' ' -f1)
  python3 - "$expected_hash" <<'WORKER_VERIFY'
import hashlib, json, sys
from pathlib import Path
root = Path("/var/lib/mobius-rebuild")
active = json.loads((root / "workers.json").read_text())["active"]
assert active["sha256"] == sys.argv[1], "active worker is not the reviewed release's worker"
assert hashlib.sha256((root / "workers" / active["file"]).read_bytes()).hexdigest() == sys.argv[1]
print("release replay: active worker bytes match the published target source")
WORKER_VERIFY
fi
if [[ $TRANSCRIPTS == 1 ]]; then
  tprobe dump >"$TPROOF/previous.json"
  tsame "the previous release serves the target's transcripts" "$TPROOF/target.json" "$TPROOF/previous.json"
  tprobe write-old >/dev/null
  tprobe dump >"$TPROOF/previous-wrote.json"
  echo "5. the app requests the target release once more"
  nonce=$(queue "$TARGET")
  wait_status "$nonce" succeeded
  replaced_with "$TARGET"
  tconverge
  tprobe dump >"$TPROOF/final.json"
  [[ $(tprobe mirror-exact) == '{"differ": []}' ]] || fail "a re-converted chat's legacy bytes are not its rows"
  tsame "the target re-converted the previous release's writes" "$TPROOF/previous-wrote.json" "$TPROOF/final.json"
  echo "host helper: transcripts survived target -> previous -> target through the real worker"
fi
echo "host helper: worker revision $seeded -> $expected arrived with the image, replaced the container, and is active"
