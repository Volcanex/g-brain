"""The live-vault guard in conftest: the suite root is temp, and the guard trips.

Each tripping run is a separate pytest session over a copy of conftest, so planting a
live path cannot refuse the rest of this suite. The live vault there is a made-up one
under tmp_path, named once through BRAIN_ROOT and once through HOME. The path is only
a string, nothing opens it.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import g_config

TESTS = Path(__file__).resolve().parent

_PROBE = '''
import sys, types
from pathlib import Path

HERE = Path(__file__).parent


def test_1_plants_a_live_path():
    m = types.ModuleType("planted")
    m.__file__ = str(HERE / "planted.py")
    m.TODOS_DIR = "@PLANTED@"
    sys.modules["planted"] = m


def test_2_is_refused():
    (HERE / "ran").write_text("the guard let this test run")
'''


def _run(tmp_path, planted, **env):
    shutil.copy(TESTS / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "test_probe.py").write_text(_PROBE.replace("@PLANTED@", str(planted)))
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           str(tmp_path)], cwd=tmp_path, capture_output=True, text=True,
                          env={**os.environ, **env})


def test_suite_root_is_a_temp_dir():
    live = str(Path.home() / "brain")
    for value in (os.environ["BRAIN_ROOT"], str(g_config.BRAIN_ROOT),
                  str(g_config.VOICE_TRANSCRIPT_DIR)):
        assert not (value == live or value.startswith(live + os.sep)), value
    assert Path(os.environ["BRAIN_ROOT"]).resolve().is_relative_to(
        Path(tempfile.gettempdir()).resolve())


@pytest.mark.parametrize("via", ["BRAIN_ROOT", "HOME"])
def test_guard_refuses_tests_once_a_module_holds_a_live_path(tmp_path, via):
    if via == "BRAIN_ROOT":
        vault, env = tmp_path / "vault", {"BRAIN_ROOT": str(tmp_path / "vault")}
    else:
        vault, env = tmp_path / "home" / "brain", {"HOME": str(tmp_path / "home")}
    r = _run(tmp_path, vault / "todos", **env)
    assert r.returncode != 0, r.stdout
    assert "1 passed" in r.stdout and "1 error" in r.stdout, r.stdout
    assert f"refusing to run: planted.TODOS_DIR = {vault / 'todos'}" in r.stdout, r.stdout
    assert not (tmp_path / "ran").exists()


def test_guard_lets_a_clean_session_run(tmp_path):
    r = _run(tmp_path, tmp_path / "todos")
    assert r.returncode == 0, r.stdout
    assert (tmp_path / "ran").exists()
