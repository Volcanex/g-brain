"""Reflexes repair locally, verify, and leave failed repairs to transition paging."""
import importlib.machinery
import importlib.util
import errno
import json
import os
import subprocess
from collections import namedtuple
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

import pytest


Usage = namedtuple("Usage", "total used free")
GIB = 1024 ** 3


def usage(pct):
    return Usage(100 * GIB, pct * GIB, (100 - pct) * GIB)


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    # Import after conftest has replaced the live vault paths.
    import brain_status as status
    from g_spine import reflexes

    svc = {"id": "worker", "unit": "worker", "host": "here"}
    manifest = {"hosts": [{"id": "here"}], "services": [svc]}
    monkeypatch.setattr(status, "BRAIN_ROOT", tmp_path)
    monkeypatch.setattr(status, "BRAIN_HOST", "here")
    monkeypatch.setattr(status, "GCORE_REPO", "owner/instance")
    monkeypatch.setattr(status, "_client", lambda: nullcontext(None))
    monkeypatch.setattr(status, "_fetch_file", lambda *args: json.dumps(manifest))
    monkeypatch.setattr(status, "_dependency_repo_map", lambda: {})
    monkeypatch.setattr(status, "_build_health_cache", lambda *args: {})
    monkeypatch.setattr(status.brain_alerts, "ALERTS_STATE", tmp_path / "alerts.json")
    monkeypatch.setattr(status.brain_alerts, "STATE_DIR", tmp_path)
    monkeypatch.setattr(status.brain_alerts, "dry_run", lambda: False)
    def fake_send(level, source, msg, dedup_key=None):
        if dedup_key:
            status.brain_alerts._stamp(dedup_key)
        return {"ok": True}

    send = Mock(side_effect=fake_send)
    monkeypatch.setattr(status.brain_alerts, "send", send)
    monkeypatch.setattr(reflexes.time, "sleep", Mock())
    clock = [1_800_000_000.0]
    monkeypatch.setattr(reflexes.time, "time", lambda: clock[0])
    monkeypatch.setattr(reflexes.shutil, "disk_usage", Mock(return_value=usage(50)))
    command = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(reflexes.subprocess, "run", command)
    monkeypatch.setattr(status, "_check_unit", lambda *args: ("down", "systemctl: inactive"))

    def row(state="down", detail="systemctl: inactive"):
        return {**svc, "method": "unit", "state": state, "detail": detail}

    probe = Mock(side_effect=lambda *args: row())
    monkeypatch.setattr(status, "probe", probe)
    out = tmp_path / "status"
    out.mkdir()
    snapshot = out / "snapshot.json"
    snapshot.write_text(json.dumps({"services": [row("up")]}))
    return status, reflexes, svc, manifest, row, probe, command, send, clock, out


def sudo_calls(command):
    return [c.args[0] for c in command.call_args_list
            if c.args[0][0] == "sudo"]


@pytest.mark.parametrize("unit_state", ["inactive", "failed"])
def test_dead_unit_started_and_healed_without_page(watcher, monkeypatch, unit_state):
    status, reflexes, svc, manifest, row, probe, command, send, clock, out = watcher
    monkeypatch.setattr(status, "_check_unit", lambda *args: ("down", f"systemctl: {unit_state}"))
    probe.side_effect = [row(), row("up", "systemctl: active")]
    # Even an earlier page must not produce a recovery page for a reflex heal.
    status.brain_alerts.save_state({"status:worker": "earlier"})
    result = status.run()["services"][0]
    assert result["state"] == "up"
    assert result["healed"] == "started worker.service"
    assert sudo_calls(command) == [["sudo", "-n", "/usr/local/sbin/g-reflex", "start", "worker.service"]]
    reflexes.time.sleep.assert_called_once_with(3)
    assert probe.call_count == 2
    send.assert_not_called()
    assert "status:worker" not in status.brain_alerts.load_state()
    record = json.loads((out / "reflex-log.jsonl").read_text())
    assert record["remedy"] == "start" and record["target"] == "worker.service"
    assert record["before"] == "down" and record["after"] == "up"
    assert record["ts"] and record["detail"]


@pytest.mark.parametrize("outcome", [
    subprocess.CompletedProcess([], 1, "", "start failed"),
    FileNotFoundError("sudo missing"),
    subprocess.TimeoutExpired("sudo", 30),
])
def test_failed_start_is_limited_and_pages_after_second_attempt(watcher, outcome):
    status, reflexes, svc, manifest, row, probe, command, send, clock, out = watcher
    if isinstance(outcome, Exception):
        command.side_effect = outcome
    else:
        command.return_value = outcome
    first = status.run()["services"][0]
    assert first["state"] == "down" and first["reflex_pending"]
    send.assert_not_called()
    clock[0] += 300
    second = status.run()["services"][0]
    assert second["state"] == "down"
    assert "start attempted twice, still down" in second["detail"]
    send.assert_called_once()
    assert send.call_args.args[:2] == ("error", "worker")
    assert send.call_args.kwargs == {"dedup_key": "status:worker"}
    for _ in range(3):
        clock[0] += 300
        status.run()
    assert len(sudo_calls(command)) == 2
    send.assert_called_once()
    state = json.loads((out / "reflexes.json").read_text())
    assert len(state["start:worker.service"]["attempts"]) == 2
    assert len((out / "reflex-log.jsonl").read_text().splitlines()) == 2
    clock[0] += 6 * 3600
    status.run()
    assert len(sudo_calls(command)) == 3


@pytest.mark.parametrize("active", ["active", "activating", "deactivating", "unknown"])
def test_running_or_unconfirmed_unit_never_started(watcher, monkeypatch, active):
    status, reflexes, svc, manifest, row, probe, command, send, clock, out = watcher
    monkeypatch.setattr(status, "_check_unit", lambda *args: (
        "up" if active == "active" else "down", f"systemctl: {active}"))
    result = status.run()["services"][0]
    assert result["state"] == "down"
    assert sudo_calls(command) == []
    send.assert_called_once()


def test_healthy_unit_never_touched(watcher):
    status, reflexes, svc, manifest, row, probe, command, send, clock, out = watcher
    probe.side_effect = lambda *args: row("up", "systemctl: active")
    status.run()
    command.assert_not_called()
    send.assert_not_called()


def test_attempt_limit_is_a_rolling_six_hour_window(watcher):
    status, reflexes, svc, manifest, row, probe, command, send, clock, out = watcher
    start = clock[0]
    command.return_value = subprocess.CompletedProcess([], 1, "", "failed")
    status.run()
    clock[0] += 300
    status.run()
    clock[0] = start + 6 * 3600 - 1
    status.run()
    assert len(sudo_calls(command)) == 2
    clock[0] += 1
    status.run()
    assert len(sudo_calls(command)) == 3
    clock[0] += 1
    status.run()
    assert len(sudo_calls(command)) == 3


@pytest.mark.parametrize("contents", ["{broken", "[]", '{"start:worker.service": {"attempts": "bad"}}'])
def test_invalid_state_never_resets_the_attempt_budget(watcher, contents):
    status, reflexes, svc, manifest, row, probe, command, send, clock, out = watcher
    (out / "reflexes.json").write_text(contents)
    result = status.run()["services"][0]
    assert result["state"] == "down"
    assert "reflex state unavailable" in result["detail"]
    command.assert_not_called()
    send.assert_called_once()


def test_remote_unit_untouched(watcher, monkeypatch):
    status, reflexes, svc, manifest, row, probe, command, send, clock, out = watcher
    svc["host"] = "remote"
    unit_check = Mock(side_effect=AssertionError("remote must not be checked for repair"))
    monkeypatch.setattr(status, "_check_unit", unit_check)
    status.run()
    command.assert_not_called()
    unit_check.assert_not_called()
    send.assert_called_once()


@pytest.mark.parametrize("pct", [85, 95])
def test_disk_clean_remeasures_and_records_bytes_without_page(watcher, pct):
    status, reflexes, svc, manifest, row, probe, command, send, clock, out = watcher
    probe.side_effect = lambda *args: row("up")
    reflexes.shutil.disk_usage.side_effect = [usage(pct), usage(80)]
    command.return_value = subprocess.CompletedProcess([], 0, str((pct - 80) * GIB), "")
    status.brain_alerts.save_state({"status:disk": "earlier"})
    disk = status.run()["disk"]
    assert disk == {"used_pct": 80.0, "free_gb": 20.0, "freed_bytes": (pct - 80) * GIB}
    assert sudo_calls(command) == [["sudo", "-n", "/usr/local/sbin/g-reflex", "clean-disk"]]
    assert "status:disk" not in status.brain_alerts.load_state()
    send.assert_not_called()
    record = json.loads((out / "reflex-log.jsonl").read_text())
    assert record["before"]["used_pct"] == pct
    assert record["after"]["used_pct"] == 80
    assert "freed" in record["detail"]


def test_disk_cleanup_can_recover_when_recording_attempt_hits_enospc(watcher, monkeypatch):
    status, reflexes, svc, manifest, row, probe, command, send, clock, out = watcher
    probe.side_effect = lambda *args: row("up")
    reflexes.shutil.disk_usage.side_effect = [usage(100), usage(80)]
    saved = reflexes.Reflexes._save
    attempts = []

    def full_once(self):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError(errno.ENOSPC, "disk full")
        saved(self)

    monkeypatch.setattr(reflexes.Reflexes, "_save", full_once)
    disk = status.run()["disk"]
    assert disk["used_pct"] == 80
    assert disk["freed_bytes"] == 20 * GIB
    assert len(sudo_calls(command)) == 1
    assert len(json.loads((out / "reflexes.json").read_text())["clean-disk:/"]["attempts"]) == 1
    assert (out / "reflex-log.jsonl").exists()
    send.assert_not_called()


def test_disk_pages_once_after_cleaning_and_resets_under_85(watcher):
    status, reflexes, svc, manifest, row, probe, command, send, clock, out = watcher
    probe.side_effect = lambda *args: row("up")
    reflexes.shutil.disk_usage.side_effect = [usage(95), usage(92), usage(92), usage(92), usage(92), usage(84)]
    command.return_value = subprocess.CompletedProcess([], 0, str(3 * GIB), "")
    status.run()
    send.assert_called_once()
    assert send.call_args.args[:2] == ("error", "disk")
    assert send.call_args.kwargs == {"dedup_key": "status:disk"}
    status.run()
    status.run()
    assert len(sudo_calls(command)) == 2
    send.assert_called_once()
    status.run()
    assert "status:disk" not in status.brain_alerts.load_state()


def test_stale_basic_memory_restarts_then_heals_on_next_run(watcher):
    status, reflexes, svc, manifest, row, probe, command, send, clock, out = watcher
    svc.update(id="basic-memory", unit="basic-memory")
    probe.side_effect = lambda *args: row("warn", "TCP 8765 open; search freshness: canary checksum differs from disk")
    first = status.run()["services"][0]
    assert first["state"] == "warn" and first["reflex_pending"]
    assert "healed" not in first
    assert sudo_calls(command) == [["sudo", "-n", "/usr/local/sbin/g-reflex", "restart", "basic-memory"]]
    assert probe.call_count == 1
    reflexes.time.sleep.assert_not_called()
    send.assert_not_called()
    probe.side_effect = lambda *args: row("up", "TCP 8765 open; search freshness: canary checksum matches")
    healed = status.run()["services"][0]
    assert healed["healed"] == "restarted basic-memory"
    assert len(sudo_calls(command)) == 1
    send.assert_not_called()


def test_basic_memory_not_healed_pages_after_two_verified_restarts(watcher):
    status, reflexes, svc, manifest, row, probe, command, send, clock, out = watcher
    svc.update(id="basic-memory", unit="basic-memory")
    probe.side_effect = lambda *args: row("warn", "TCP 8765 open; search freshness: watcher reports stopped")
    status.run()
    status.run()
    send.assert_not_called()
    result = status.run()["services"][0]
    assert result["state"] == "warn"
    assert "restart attempted twice, still warn" in result["detail"]
    assert len(sudo_calls(command)) == 2
    send.assert_called_once()


def test_basic_memory_tcp_down_uses_start_instead_of_restart(watcher):
    status, reflexes, svc, manifest, row, probe, command, send, clock, out = watcher
    svc.update(id="basic-memory", unit="basic-memory")
    probe.side_effect = [row("down", "TCP 8765: ConnectionRefusedError; search freshness: canary checksum differs from disk"), row("up")]
    status.run()
    assert sudo_calls(command) == [["sudo", "-n", "/usr/local/sbin/g-reflex", "start", "basic-memory.service"]]


@pytest.fixture
def wrapper(monkeypatch):
    monkeypatch.setenv("PATH", os.environ["PATH"])
    path = Path(__file__).resolve().parents[1] / "deploy" / "g-reflex"
    loader = importlib.machinery.SourceFileLoader("reflex_wrapper", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    command = Mock(return_value=subprocess.CompletedProcess([], 0, "enabled\n", ""))
    command.side_effect = lambda cmd, **kw: (
        subprocess.CompletedProcess(cmd, 3, "inactive\n", "")
        if cmd[:2] == ["systemctl", "is-active"] else command.return_value)
    monkeypatch.setattr(module.subprocess, "run", command)
    return module, command


@pytest.mark.parametrize("unit", ["worker", "worker.service", "worker@one.service"])
def test_wrapper_starts_only_enabled_services(wrapper, unit):
    module, command = wrapper
    assert module.main(["start", unit]) == 0
    target = unit if unit.endswith(".service") else unit + ".service"
    assert [c.args[0] for c in command.call_args_list] == [
        ["systemctl", "is-enabled", target],
        ["systemctl", "is-active", target],
        ["systemctl", "reset-failed", target],
        ["systemctl", "start", target],
    ]


@pytest.mark.parametrize("args", [[], ["stop", "worker"], ["restart", "worker"],
    ["restart", "basic-memory", "extra"], ["clean-disk", "extra"],
    ["start", "reboot.target"], ["start", "poweroff.target"], ["start", "worker.socket"],
    ["start", "../worker"], ["start", "worker;id"], ["start", "-worker"],
    ["start", "worker", "extra"]])
def test_wrapper_rejects_every_other_verb_or_argument(wrapper, args):
    module, command = wrapper
    assert module.main(args) != 0
    command.assert_not_called()


@pytest.mark.parametrize("enabled", ["disabled", "static", "indirect", "masked", "enabled-runtime"])
def test_wrapper_rejects_units_not_enabled(wrapper, enabled):
    module, command = wrapper
    command.return_value = subprocess.CompletedProcess([], 1, enabled, "")
    assert module.main(["start", "worker"]) != 0
    assert command.call_count == 1


def test_wrapper_never_starts_an_active_unit(wrapper):
    module, command = wrapper
    command.side_effect = [subprocess.CompletedProcess([], 0, "enabled\n", ""),
                           subprocess.CompletedProcess([], 0, "active\n", "")]
    assert module.main(["start", "worker"]) == 0
    assert command.call_count == 2


def test_wrapper_restart_is_basic_memory_only(wrapper):
    module, command = wrapper
    assert module.main(["restart", "basic-memory"]) == 0
    assert command.call_args.args[0] == ["systemctl", "restart", "basic-memory.service"]


def _aged(path, when):
    os.utime(path, (when, when), follow_symlinks=False)


def test_wrapper_clean_is_bounded_and_tolerates_missing_tools(wrapper, tmp_path, monkeypatch, capsys):
    module, command = wrapper
    clock = 1_800_000_000.0
    old = clock - 15 * 86400
    root = tmp_path / "tmp"
    project = root / "claude-0" / "-root"
    stale = project / "stale-session"
    live = project / "live-session"
    (stale / "scratchpad").mkdir(parents=True)
    (live / "scratchpad").mkdir(parents=True)
    (stale / "scratchpad" / "a.txt").write_text("old")
    (live / "scratchpad" / "b.txt").write_text("new")
    loose = root / "claude-0" / "loose.png"
    loose.write_text("old")
    other = root / "other"
    other.mkdir()
    for path in (stale / "scratchpad" / "a.txt", stale / "scratchpad", stale, live / "scratchpad", live, loose, other):
        _aged(path, old)
    _aged(live / "scratchpad" / "b.txt", clock)
    pip = tmp_path / "pip"
    pip.mkdir()
    monkeypatch.setattr(module, "PIP_CACHES", [pip])
    monkeypatch.setattr(module, "SCRATCH_ROOT", root)
    monkeypatch.setattr(module.time, "time", lambda: clock)
    monkeypatch.setattr(module.shutil, "disk_usage", Mock(side_effect=[usage(95), usage(80)]))
    command.side_effect = FileNotFoundError("tool absent")
    assert module.main(["clean-disk"]) == 0
    assert capsys.readouterr().out.strip() == str(15 * GIB)
    assert not pip.exists() and not stale.exists() and not loose.exists()
    # A session whose own dir is old but whose files are fresh is live.
    assert (live / "scratchpad" / "b.txt").exists() and other.exists()
    assert [c.args[0] for c in command.call_args_list] == [
        ["journalctl", "--vacuum-size=1G"], ["apt-get", "clean"],
        ["docker", "builder", "prune", "-f"], ["docker", "image", "prune", "-f"],
    ]


def test_wrapper_cleanup_never_follows_symlinks(wrapper, tmp_path, monkeypatch):
    module, command = wrapper
    protected = tmp_path / "protected"
    protected.mkdir()
    pip = protected / "pip"
    job = protected / "job"
    pip.mkdir()
    job.mkdir()
    old = module.time.time() - 30 * 86400
    _aged(job, old)
    _aged(protected, old)
    redirected = tmp_path / "redirected"
    redirected.symlink_to(protected, target_is_directory=True)
    root = tmp_path / "tmp"
    root.mkdir()
    (root / "claude-0").symlink_to(protected, target_is_directory=True)
    monkeypatch.setattr(module, "PIP_CACHES", [redirected / "pip"])
    monkeypatch.setattr(module, "SCRATCH_ROOT", root)
    monkeypatch.setattr(module.shutil, "disk_usage", Mock(return_value=usage(50)))
    assert module.main(["clean-disk"]) == 0
    assert pip.exists() and job.exists()
