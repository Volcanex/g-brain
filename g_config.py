"""g_config: the one place g-brain's components resolve paths and secrets.

Every component (spine, todos, pulse, brain, voice) used to compute BRAIN_ROOT and its
own .env lookup on its own, anchored on its own file. Moving a file into a package
would then silently change where it looks. This module is anchored on the repo root
instead, so the answers stay the same wherever the caller lives.

Path values are computed from the environment each time they are read. A module that
copies one at import time (`BRAIN_ROOT = g_config.BRAIN_ROOT`) therefore sees the env
as it was when that module was imported, exactly as the per-module definitions did.
That keeps the tests' "set BRAIN_ROOT, then import" pattern working.

The operator's instance (gp-core, or the legacy gcore) holds the manifest and the
secrets passport. It is found the way g-env's context/instance.py finds it, copied
rather than imported across repos, so keep the order identical.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def _instance(kind: str = "dir") -> Path | None:
    """The instance dir, or its passport ("env") or "manifest" path. A per-file
    variable wins, then GCORE_DIR, GSITE_GCORE_DIR or G_INSTANCE, then gp-core or
    gcore beside this repo. None when there is no instance."""
    variable, name = {"env": ("GCORE_ENV", ".env"),
                      "manifest": ("GCORE_MANIFEST", "manifest.json")}.get(kind, (None, None))
    if variable and os.environ.get(variable):
        return Path(os.environ[variable])
    found = next((Path(os.environ[v]) for v in ("GCORE_DIR", "GSITE_GCORE_DIR", "G_INSTANCE")
                  if os.environ.get(v)), None)
    if found is None:
        found = next((REPO_ROOT.parent / n for n in ("gp-core", "gcore")
                      if (REPO_ROOT.parent / n).is_dir()), None)
    return found / name if found and name else found


# Secret homes, in priority order after the live environment. Services wire env via
# systemd EnvironmentFile, but the spine runs under units (and users) that do not all
# load the same file, so it falls back to reading these in-process.
_ENV_FILES = [
    REPO_ROOT / ".env",
    REPO_ROOT / "voice" / ".env",
    *filter(None, [_instance("env")]),
]


def env(key: str) -> str:
    """Resolve a secret: live environment first, then the known .env files.
    Reads the files in-process only, never echoes them."""
    v = os.environ.get(key)
    if v:
        return v
    for f in _ENV_FILES:
        try:
            for line in f.read_text().splitlines():
                line = line.strip()
                if line.startswith(key + "="):
                    return line[len(key) + 1:].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


# The vault and the other host values resolve through env(), so a root shell with no
# BRAIN_ROOT reaches the same vault as the units, from this host's .env.
def _brain_root() -> Path:
    return Path(env("BRAIN_ROOT") or Path.home() / "brain").expanduser()


def _manifest_path() -> Path:
    # With no instance the path does not exist, and readers treat that as no manifest.
    return _instance("manifest") or REPO_ROOT.parent / "gp-core" / "manifest.json"


def _instance_repo() -> str:
    """owner/name of the instance on its git host: G_INSTANCE_REPO, else parsed from the
    instance checkout's origin remote. Empty when neither exists.

    The git config is read as a file rather than through `git remote get-url`: the units
    run as a user who does not own the checkout, and git refuses such a repo as unsafe.
    The URL can carry credentials, so only the last two path parts ever leave here."""
    if os.environ.get("G_INSTANCE_REPO"):
        return os.environ["G_INSTANCE_REPO"]
    root = _instance()
    try:
        text = (root / ".git" / "config").read_text() if root else ""
    except OSError:
        return ""
    section = re.search(r'^\s*\[remote\s+"origin"\]([^\[]*)', text, re.M)
    url = re.search(r"^\s*url\s*=\s*(\S+)", section.group(1), re.M) if section else None
    parts = re.split(r"[/:]", url.group(1).rstrip("/").removesuffix(".git")) if url else []
    return "/".join(parts[-2:]) if len(parts) >= 2 and all(parts[-2:]) else ""


def _user_agent() -> str:
    """g-brain/<version>, plus the operator's contact URL when BRAIN_CONTACT gives one."""
    try:
        version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["version"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        version = "0"
    contact = env("BRAIN_CONTACT")
    return f"g-brain/{version}" + (f" (+{contact})" if contact else "")


_VALUES = {
    "BRAIN_ROOT": _brain_root,
    "PULSE_DIR": lambda: _brain_root() / ".pulse",
    "STATE_DIR": lambda: _brain_root() / ".pulse" / "state",
    "TODOS_DIR": lambda: _brain_root() / "todos",
    "STATUS_SNAPSHOT": lambda: _brain_root() / "status" / "snapshot.json",
    "LINEAGE_DIR": lambda: _brain_root() / "lineage",
    "NERVES_DIR": lambda: _brain_root() / "nerves",
    "VOICE_TRANSCRIPT_DIR": lambda: Path(
        os.environ.get("VOICE_TRANSCRIPT_DIR", _brain_root() / ".pulse" / "state" / "voice")),
    "MANIFEST_PATH": _manifest_path,
    "MANIFEST_REPO": _instance_repo,
    # The manifest id of the host the brain runs on. Unset means every service is local.
    "BRAIN_HOST": lambda: env("BRAIN_HOST"),
    # basic-memory's search index, in its own default place unless the env names it.
    "BRAIN_MEMORY_DB": lambda: Path(
        env("BRAIN_MEMORY_DB") or Path.home() / ".basic-memory" / "memory.db").expanduser(),
    "USER_AGENT": _user_agent,
}


def __getattr__(name: str):
    try:
        return _VALUES[name]()
    except KeyError:
        raise AttributeError(f"module 'g_config' has no attribute {name!r}") from None


def __dir__():
    return sorted(set(globals()) | set(_VALUES))
