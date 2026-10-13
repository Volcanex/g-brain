"""Run vault sync against temporary local repositories, never the live vault."""

import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parent.parent / "sync_vault.sh"


def _git(path, env, *args):
    return subprocess.run(
        ["git", *args], cwd=path, env=env, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _hook(path):
    path.write_text("#!/bin/sh\nexit 1\n")
    path.chmod(0o755)


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    origin = tmp_path / "origin.git"
    root.mkdir()
    env = {**os.environ, "BRAIN_ROOT": str(root),
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    _git(tmp_path, env, "init", "--bare", "--initial-branch=main", str(origin))
    _git(root, env, "init", "--initial-branch=main")
    _git(root, env, "config", "user.name", "Vault test")
    _git(root, env, "config", "user.email", "vault@example.test")
    (root / "note.md").write_text("original note\n")
    _git(root, env, "add", "-A")
    _git(root, env, "commit", "-m", "Create test vault")
    _git(root, env, "remote", "add", "origin", str(origin))
    _git(root, env, "push", "-u", "origin", "main")
    return root, origin, env


def _sync(env):
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=15,
    )


def test_commit_failure_exits_nonzero(vault):
    root, origin, env = vault
    before = _git(origin, env, "rev-parse", "main")
    _hook(root / ".git" / "hooks" / "pre-commit")
    (root / "note.md").write_text("new note\n")

    result = _sync(env)

    assert result.returncode != 0
    assert "brain-vault: commit failed" in result.stderr
    assert _git(root, env, "rev-parse", "HEAD") == before
    assert _git(origin, env, "rev-parse", "main") == before
    assert _git(root, env, "status", "--porcelain")


def test_add_failure_exits_nonzero(vault):
    root, origin, env = vault
    before = _git(origin, env, "rev-parse", "main")
    (root / "note.md").write_text("new note\n")
    (root / ".git" / "index.lock").touch()

    result = _sync(env)

    assert result.returncode != 0
    assert "brain-vault: add failed" in result.stderr
    assert _git(root, env, "rev-parse", "HEAD") == before
    assert _git(origin, env, "rev-parse", "main") == before


def test_nothing_to_commit_exits_zero(vault):
    root, origin, env = vault
    before = _git(root, env, "rev-parse", "HEAD")
    _hook(root / ".git" / "hooks" / "pre-commit")

    result = _sync(env)

    assert result.returncode == 0, result.stderr
    assert "brain-vault: level" in result.stdout
    assert _git(root, env, "rev-parse", "HEAD") == before
    assert _git(origin, env, "rev-parse", "main") == before


def test_changes_are_committed_and_pushed(vault):
    root, origin, env = vault
    before = _git(root, env, "rev-parse", "HEAD")
    (root / "note.md").write_text("new note\n")

    result = _sync(env)

    assert result.returncode == 0, result.stderr
    assert "brain-vault: pushed 1 commit(s)" in result.stdout
    assert _git(root, env, "rev-parse", "HEAD") != before
    assert _git(origin, env, "show", "main:note.md") == "new note"
    assert not _git(root, env, "status", "--porcelain")


def test_push_failure_exits_nonzero(vault):
    root, origin, env = vault
    before = _git(origin, env, "rev-parse", "main")
    _hook(origin / "hooks" / "pre-receive")
    (root / "note.md").write_text("new note\n")

    result = _sync(env)

    assert result.returncode != 0
    assert "brain-vault: push failed with 1 commit(s) waiting" in result.stderr
    assert _git(root, env, "rev-parse", "HEAD") != before
    assert _git(origin, env, "rev-parse", "main") == before
