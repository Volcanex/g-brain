"""A service with `check` is judged by the far host; the spine only carries its state."""
from types import SimpleNamespace

import pytest

HOSTS = {"far": {"id": "far", "user": "ops", "hostname": "192.0.2.7"}, "here": {"id": "here"}}
SVC = {"id": "far-disk", "repo": "instance", "host": "far", "port": None, "domain": None,
       "unit": None, "health_url": None, "check": "disk"}


@pytest.fixture
def status(monkeypatch):
    import brain_status as status

    monkeypatch.setattr(status, "BRAIN_HOST", "here")
    return status


@pytest.mark.parametrize("printed,state,detail", [
    ("up 70% of / used, 45 GiB free\n", "up", "70% of / used, 45 GiB free"),
    ("warn 86% of / used\nsecond line ignored", "warn", "86% of / used"),
    ("down baseline-app-1 exited", "down", "baseline-app-1 exited"),
    ("up", "up", "up"),
    ("unknown could not tell: RuntimeError", "unknown", "remote check gave no state: could not tell: RuntimeError"),
    ("", "unknown", "remote check gave no state: no answer"),
])
def test_far_host_state_is_carried_as_printed(status, monkeypatch, printed, state, detail):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout=printed, returncode=0)

    monkeypatch.setattr(status.subprocess, "run", run)
    r = status.probe(SVC, HOSTS)
    assert (r["state"], r["detail"], r["method"]) == (state, detail, "check@far")
    assert calls == [["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", "ops@192.0.2.7", "check", "disk"]]


def test_unreachable_host_is_unknown_not_down(status, monkeypatch):
    def run(cmd, **kwargs):
        raise status.subprocess.TimeoutExpired(cmd, 60)

    monkeypatch.setattr(status.subprocess, "run", run)
    r = status.probe(SVC, HOSTS)
    assert (r["state"], r["detail"]) == ("unknown", "remote check failed: TimeoutExpired")


def test_local_host_runs_nothing(status, monkeypatch):
    monkeypatch.setattr(status.subprocess, "run", lambda *a, **k: pytest.fail("ran a command"))
    r = status.probe({**SVC, "host": "here"}, HOSTS)
    assert r["state"] == "unknown" and r["method"] == "check@here"
