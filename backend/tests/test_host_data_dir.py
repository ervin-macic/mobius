"""The host helper must reach the app's real /data while no container runs.

Production 2026-10-09 and 2026-10-12: /data was a ``local`` volume created
with bind options. Docker mounts such a volume at its Mountpoint only while a
container uses it, so once a replacement removed the old container, the
installer-recorded Mountpoint was a stale directory on the root filesystem.
The request watcher, status publication and the rollback ledger rearm all
silently used that hidden directory, and recovery could not prove the chat
handoff. These tests pin the stable resolution and the refusal paths.
"""
import json
import os
import subprocess

import pytest

from tests.test_mobius_rebuild_host import host
from tests.test_host_admission_configuration import topology


def _volume_docker(volume: dict, *, container: dict | None = None, resolved: dict | None = None):
    calls = []

    def command(args, **kwargs):
        args = list(args)
        calls.append(args)
        if args[:3] == ["docker", "volume", "inspect"]:
            assert args[-1] == volume["Name"]
            return subprocess.CompletedProcess(args, 0, json.dumps(volume), "")
        if args[:3] == ["docker", "container", "inspect"] and container is not None:
            return subprocess.CompletedProcess(args, 0, json.dumps(container["Mounts"]), "")
        if args[1:3] == ["compose", "-p"] and resolved is not None:
            return subprocess.CompletedProcess(args, 0, json.dumps(resolved), "")
        raise AssertionError(f"unexpected Docker call {args}")

    return command, calls


def _bind_option_volume(tmp_path):
    device = tmp_path / "srv-data"
    mountpoint = tmp_path / "docker-volumes" / "app_data" / "_data"
    device.mkdir()
    # While no container uses the volume this is an ordinary, unrelated
    # directory: exactly what the old installer recorded as data_dir.
    mountpoint.mkdir(parents=True)
    return device, mountpoint, {
        "Name": "mobius_app_data", "Driver": "local", "Mountpoint": str(mountpoint),
        "Options": {"type": "none", "o": "bind", "device": str(device)},
    }


def test_bind_option_volume_resolves_to_its_device_not_the_transient_mountpoint(tmp_path, monkeypatch):
    device, mountpoint, volume = _bind_option_volume(tmp_path)
    command, _ = _volume_docker(volume)
    monkeypatch.setattr(host, "docker_command", command)
    assert host.persistent_data_dir("volume", "mobius_app_data") == os.path.realpath(device)
    assert host.persistent_data_dir("volume", "mobius_app_data") != str(mountpoint)


def test_plain_local_volume_and_bind_mount_keep_their_own_directory(tmp_path, monkeypatch):
    mountpoint = tmp_path / "plain" / "_data"
    mountpoint.mkdir(parents=True)
    command, _ = _volume_docker({"Name": "plain", "Driver": "local",
                                 "Mountpoint": str(mountpoint), "Options": None})
    monkeypatch.setattr(host, "docker_command", command)
    assert host.persistent_data_dir("volume", "plain") == os.path.realpath(mountpoint)
    assert host.persistent_data_dir("bind", str(tmp_path / "plain")) == os.path.realpath(tmp_path / "plain")


@pytest.mark.parametrize("volume", [
    {"Driver": "local", "Options": {"type": "nfs", "o": "addr=10.0.0.1", "device": ":/export"}},
    {"Driver": "local", "Options": {"type": "tmpfs", "device": "tmpfs"}},
    {"Driver": "local", "Options": {"type": "none", "o": "ro", "device": "/srv"}},
    {"Driver": "rexray", "Options": None},
])
def test_volumes_the_host_cannot_reach_while_stopped_are_refused(tmp_path, monkeypatch, volume):
    command, _ = _volume_docker({"Name": "data", "Mountpoint": str(tmp_path), **volume})
    monkeypatch.setattr(host, "docker_command", command)
    with pytest.raises(RuntimeError):
        host.persistent_data_dir("volume", "data")


def test_missing_stable_directory_is_refused_rather_than_created(tmp_path, monkeypatch):
    _, _, volume = _bind_option_volume(tmp_path)
    volume["Options"]["device"] = str(tmp_path / "unmounted-disk")
    command, _ = _volume_docker(volume)
    monkeypatch.setattr(host, "docker_command", command)
    with pytest.raises(RuntimeError, match="not reachable"):
        host.persistent_data_dir("volume", "mobius_app_data")
    assert not (tmp_path / "unmounted-disk").exists()


def _volume_topology(tmp_path, monkeypatch, data_dir):
    config, tx, resolved = topology(tmp_path, monkeypatch)
    _, _, volume = _bind_option_volume(tmp_path)
    resolved["services"]["app"]["volumes"] = [
        {"type": "volume", "source": "app_data", "target": "/data"}]
    resolved["volumes"] = {"app_data": {"name": "mobius_app_data"}}
    command, _ = _volume_docker(volume, resolved=resolved)
    monkeypatch.setattr(host, "docker_command", command)
    config["data_dir"] = data_dir(volume)
    return config, tx


def test_replacement_refuses_before_drain_when_config_names_the_transient_mountpoint(tmp_path, monkeypatch):
    # An installer before helper protocol 6 recorded Docker's Mountpoint. The
    # worker must refuse up front (wrapped_configuration runs before any
    # transaction or drain) and say how to repair, not use the hidden path.
    config, tx = _volume_topology(tmp_path, monkeypatch, lambda v: v["Mountpoint"])
    with pytest.raises(RuntimeError, match="rerun scripts/install-rebuild-helper.sh"):
        host.wrapped_configuration(config, tx, "target", "b" * 32, "sha256:" + "c" * 64)


def test_replacement_accepts_the_stable_device_for_a_bind_option_volume(tmp_path, monkeypatch):
    config, tx = _volume_topology(tmp_path, monkeypatch, lambda v: v["Options"]["device"])
    result = host.wrapped_configuration(config, tx, "target", "b" * 32, "sha256:" + "c" * 64)
    assert result["volumes"] == {"app_data": {"name": "mobius_app_data", "external": True}}


def test_installer_discovery_reports_stable_and_docker_paths(tmp_path, monkeypatch, capsys):
    device, mountpoint, volume = _bind_option_volume(tmp_path)
    container = {"Mounts": [{"Type": "volume", "Name": "mobius_app_data", "Source": str(mountpoint),
                             "Destination": "/data", "RW": True}]}
    command, _ = _volume_docker(volume, container=container)
    monkeypatch.setattr(host, "docker_command", command)
    assert host.data_source("f" * 64) == 0
    assert json.loads(capsys.readouterr().out) == {
        "data_dir": os.path.realpath(device), "mount_source": str(mountpoint)}


def test_status_reports_only_an_installer_certified_helper_protocol(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "HELPER_PROTOCOL", tmp_path / "helper-protocol")
    assert host.installed_helper_protocol(trusted_uid=os.getuid()) == 0
    host.HELPER_PROTOCOL.write_text("6\n")
    host.HELPER_PROTOCOL.chmod(0o600)
    assert host.installed_helper_protocol(trusted_uid=os.getuid()) == 6
    host.HELPER_PROTOCOL.chmod(0o666)
    assert host.installed_helper_protocol(trusted_uid=os.getuid()) == 0
    host.HELPER_PROTOCOL.chmod(0o600)
    host.HELPER_PROTOCOL.write_text("six\n")
    assert host.installed_helper_protocol(trusted_uid=os.getuid()) == 0
    control = tmp_path / "control"
    control.mkdir()
    monkeypatch.setattr(host, "STATUS", tmp_path / "status.json")
    monkeypatch.setattr(host, "installed_helper_protocol", lambda: 6)
    assert host.write_status({"control_dir": control})["helper_protocol"] == 6


def test_installer_units_and_config_use_the_stable_path_and_certify_last():
    source = host.Path(host.__file__).parent.joinpath("install-rebuild-helper.sh").read_text()
    assert 'mobius-rebuild-host.py" data-source "$CID"' in source
    assert "{{.Source}}{{end}}{{end}}')" not in source  # no raw Docker Mountpoint
    for unit in ("mobius-rebuild.service", "mobius-rebuild.path", "mobius-rebuild-reconcile.service"):
        block = source[source.index(f'cat >"$STAGE/{unit}" <<EOF'):]
        block = block[:block.index("\nEOF\n")]
        assert "RequiresMountsFor=$DATA_SOURCE" in block, unit
    assert "PathChanged=$DATA_SOURCE/mobius-rebuild/inbox/request.json" in source
    # Certified only after the launcher, units and dispatch are all in place,
    # then republished so the app sees it without waiting for a reconcile.
    launcher = source.index('/usr/local/libexec/mobius-rebuild-host 0755 --')
    dispatching = source.index("systemctl enable --now mobius-rebuild.path")
    certified = source.index("/var/lib/mobius-rebuild/helper-protocol 0600 --")
    capabilities = source.rindex('"$ROOT/scripts/mobius-rebuild-host.py" publish-capabilities')
    complete = source.index("MIGRATION_COMPLETE=1")
    assert launcher < dispatching < certified < capabilities < complete
    marker = host.Path(host.__file__).parents[1] / "deployment" / "self-hosted-helper.required"
    assert f"HELPER_PROTOCOL={marker.read_text().strip()}\n" in source


def test_installer_resolves_data_paths_after_the_clean_checkout_and_before_identity():
    # Review finding: verify_admission_identity reads DATA_MOUNT_SOURCE, so a
    # helper-managed host stopped (set -u) before the variable existed. Root
    # may run the checkout's resolver only after the checkout is proven clean.
    source = host.Path(host.__file__).parent.joinpath("install-rebuild-helper.sh").read_text()
    body = source[source.index("PY_ADMISSION\n}\n"):]
    clean = body.index("diff --quiet HEAD")
    resolved = body.index("DATA_MOUNT_SOURCE=$(")
    preflight = body.index("\n  verify_admission_identity\n")
    assert clean < resolved < preflight
