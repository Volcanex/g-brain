"""Suite-wide guard: no test may resolve the live vault.

Modules read BRAIN_ROOT once, at import, and whichever test imports a module first
fixes its paths for the whole run. A test that imported voice.agent at collection
once made test_todos (which sets its own BRAIN_ROOT, then imports brain_todos) write
its fixtures into the live vault's todos/. So BRAIN_ROOT, the voice transcript dir
and the instance passport point into a session temp dir before any test module is
imported, and every test is refused while any repo module still holds a path inside a
live vault: wherever BRAIN_ROOT pointed before the overrides, and ~/brain.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


def _live_roots():
    """The vaults this run could reach without the overrides below: BRAIN_ROOT from the
    environment, BRAIN_ROOT as g_config resolves it (which also reads the repo and
    instance .env files), and ~/brain. g_config is dropped again afterwards, so the
    first real import sees the overrides. A copy of this file outside the repo, as
    test_guard runs it, has no g_config and skips that one."""
    roots = [os.environ.get("BRAIN_ROOT", ""), Path.home() / "brain"]
    try:
        import g_config
        roots.append(g_config.BRAIN_ROOT)
    except ImportError:
        pass
    sys.modules.pop("g_config", None)
    return {str(p) for r in roots if r
            for p in (Path(r).expanduser(), Path(r).expanduser().resolve())}


_LIVE = _live_roots()
_TMP = tempfile.mkdtemp(prefix="g-brain-tests-")
atexit.register(shutil.rmtree, _TMP, True)
os.environ["BRAIN_ROOT"] = _TMP
os.environ["VOICE_TRANSCRIPT_DIR"] = os.path.join(_TMP, ".pulse", "state", "voice")
# The instance passport can name the live vault too. Point it at a file that is not there.
os.environ["GCORE_ENV"] = os.path.join(_TMP, "instance.env")

_REPO = str(Path(__file__).resolve().parent.parent) + os.sep
_in_repo = {}


def _live_paths():
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if f not in _in_repo:
            _in_repo[f] = bool(f) and str(Path(f).resolve()).startswith(_REPO)
        if not _in_repo[f]:
            continue
        for attr, val in list(vars(mod).items()):
            if isinstance(val, (str, Path)):
                s = str(val)
                if any(s == live or s.startswith(live + os.sep) for live in _LIVE):
                    yield f"{name}.{attr} = {s}"


def pytest_runtest_setup(item):
    bad = sorted(set(_live_paths()))
    if bad:
        pytest.fail("live vault path held by an imported module, refusing to run: "
                    + "; ".join(bad), pytrace=False)
