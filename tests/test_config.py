"""g_config parity: the shared values match what each module computes on its own today."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import brain_alerts
import g_config

REPO = Path(__file__).resolve().parent.parent
_INSTANCE_KEYS = ("VOICE_TRANSCRIPT_DIR", "GCORE_MANIFEST", "GCORE_ENV", "GCORE_DIR",
                  "GSITE_GCORE_DIR", "G_INSTANCE", "G_INSTANCE_REPO", "BRAIN_HOST", "BRAIN_CONTACT")

# A host's own .env names its vault and host. The probes drop the tree's env files before
# any other import, so they see only the env they are given, as a fresh checkout does.
_FRESH = r"""
import g_config
g_config._ENV_FILES = [f for f in g_config._ENV_FILES if g_config.REPO_ROOT not in f.parents]
"""

# Runs in a fresh interpreter so every module computes its constants under the given env.
_PROBE = _FRESH + r"""
import json, g_config, brain_alerts, brain_pulse, brain_status, brain_todos, brain_reader
pairs = [
    ("BRAIN_ROOT", g_config.BRAIN_ROOT, brain_reader.BRAIN_ROOT),
    ("BRAIN_ROOT", g_config.BRAIN_ROOT, brain_pulse.BRAIN_ROOT),
    ("BRAIN_ROOT", g_config.BRAIN_ROOT, brain_alerts.BRAIN_ROOT),
    ("PULSE_DIR", g_config.PULSE_DIR, brain_pulse.PULSE_DIR),
    ("STATE_DIR", g_config.STATE_DIR, brain_pulse.STATE_DIR),
    ("STATE_DIR", g_config.STATE_DIR, brain_alerts.STATE_DIR),
    ("STATUS_SNAPSHOT", g_config.STATUS_SNAPSHOT, brain_pulse.STATUS_SNAPSHOT),
    ("TODOS_DIR", g_config.TODOS_DIR, brain_todos.TODOS_DIR),
    ("BRAIN_HOST", g_config.BRAIN_HOST, brain_status.BRAIN_HOST),
    ("MANIFEST_REPO", g_config.MANIFEST_REPO, brain_status.GCORE_REPO),
]
print(json.dumps([(n, str(a), str(b)) for n, a, b in pairs]))
"""


def _probe(env):
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=REPO, env=env,
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


class ModuleParity(unittest.TestCase):
    def _base_env(self, home):
        env = {k: v for k, v in os.environ.items()
               if k not in ("BRAIN_ROOT", "BRAIN_HOST", "G_INSTANCE_REPO")}
        env.update(HOME=home, PYTHONDONTWRITEBYTECODE="1")
        return env

    def test_parity_with_brain_root_set(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as root:
            env = self._base_env(home)
            env.update(BRAIN_ROOT=root, BRAIN_HOST="x", G_INSTANCE_REPO="Org/inst")
            pairs = _probe(env)
            for name, ours, theirs in pairs:
                self.assertEqual(ours, theirs, name)
            self.assertIn(("BRAIN_ROOT", root, root), [tuple(p) for p in pairs])

    def test_parity_with_brain_root_unset(self):
        with tempfile.TemporaryDirectory() as home:
            pairs = _probe(self._base_env(home))
            for name, ours, theirs in pairs:
                self.assertEqual(ours, theirs, name)
            self.assertIn(("BRAIN_ROOT", f"{home}/brain", f"{home}/brain"),
                          [tuple(p) for p in pairs])

    def test_instance_env_gives_the_vault_to_a_shell_without_one(self):
        probe = _FRESH + r"""
import json, brain_capture, brain_miner, brain_status, g_config, inlets.gmail_fetch
import outlets.remind, outlets.watch, voice.health
print(json.dumps([str(g_config.BRAIN_ROOT), str(brain_capture.BRAIN_ROOT), brain_miner.BRAIN_ROOT,
                  inlets.gmail_fetch.STATE, outlets.remind.STATE, outlets.watch.TODOS,
                  voice.health._OUTLET_STATE, str(g_config.BRAIN_MEMORY_DB), brain_status.BRAIN_HOST]))
"""
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as d:
            passport, live = Path(d) / "instance.env", Path(d) / "live"
            passport.write_text(f"BRAIN_ROOT={d}/vault\nBRAIN_MEMORY_DB={d}/memory.db\nBRAIN_HOST=x\n")
            env = {k: v for k, v in self._base_env(home).items()
                   if k not in (*_INSTANCE_KEYS, "BRAIN_MEMORY_DB")}
            env.update(GCORE_ENV=str(passport))
            run = lambda e: json.loads(subprocess.run(
                [sys.executable, "-c", probe], cwd=REPO, env=e, capture_output=True, text=True,
                check=True).stdout.strip().splitlines()[-1])
            *paths, db, host = run(env)
            for path in paths:
                self.assertTrue(path.startswith(f"{d}/vault"), path)
            self.assertEqual((db, host), (f"{d}/memory.db", "x"))
            *paths, _, _ = run({**env, "BRAIN_ROOT": str(live)})
            for path in paths:
                self.assertTrue(path.startswith(str(live)), path)

    def test_values_follow_env_at_read_time(self):
        with patch.dict(os.environ, {"BRAIN_ROOT": "/tmp/a"}):
            self.assertEqual(g_config.STATE_DIR, Path("/tmp/a/.pulse/state"))
        with patch.dict(os.environ, {"BRAIN_ROOT": "/tmp/b"}):
            self.assertEqual(g_config.TODOS_DIR, Path("/tmp/b/todos"))
            self.assertEqual(g_config.NERVES_DIR, Path("/tmp/b/nerves"))
            self.assertEqual(g_config.LINEAGE_DIR, Path("/tmp/b/lineage"))

    def test_defaults_follow_brain_root_and_name_no_host(self):
        env = {k: v for k, v in os.environ.items() if k not in _INSTANCE_KEYS}
        env["BRAIN_ROOT"] = "/tmp/a"
        with patch.object(g_config, "_ENV_FILES", []), patch.dict(os.environ, env, clear=True):
            self.assertEqual(g_config.VOICE_TRANSCRIPT_DIR, Path("/tmp/a/.pulse/state/voice"))
            self.assertEqual(g_config.BRAIN_HOST, "")
        with patch.dict(os.environ, {"BRAIN_HOST": "h"}):
            self.assertEqual(g_config.BRAIN_HOST, "h")

    def test_user_agent_names_the_package_and_an_optional_contact(self):
        version = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
        env = {k: v for k, v in os.environ.items() if k not in _INSTANCE_KEYS}
        with patch.object(g_config, "_ENV_FILES", []), patch.dict(os.environ, env, clear=True):
            self.assertEqual(g_config.USER_AGENT, f"g-brain/{version}")
            os.environ["BRAIN_CONTACT"] = "https://example.org"
            self.assertEqual(g_config.USER_AGENT, f"g-brain/{version} (+https://example.org)")

    def test_unknown_name_is_attribute_error(self):
        with self.assertRaises(AttributeError):
            g_config.NOPE  # noqa: B018


class Instance(unittest.TestCase):
    """The instance resolves in g-env's order, and the manifest and repo follow it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        env = {k: v for k, v in os.environ.items() if k not in _INSTANCE_KEYS}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(g_config, "REPO_ROOT", self.tmp / "g-brain"),
                        patch.object(g_config, "_ENV_FILES", [])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _dir(self, name, origin=None):
        d = self.tmp / name
        (d / ".git").mkdir(parents=True)
        if origin:
            (d / ".git" / "config").write_text(
                f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {origin}\n'
                '\tfetch = +refs/heads/*:refs/remotes/origin/*\n[branch "main"]\n\tremote = origin\n')
        return d

    def test_gp_core_beside_the_repo_wins_over_gcore(self):
        self._dir("gcore", origin="https://github.com/Org/old.git")
        gp = self._dir("gp-core", origin="https://github.com/Org/inst.git")
        self.assertEqual(g_config._instance(), gp)
        self.assertEqual(g_config._instance("env"), gp / ".env")
        self.assertEqual(g_config.MANIFEST_PATH, gp / "manifest.json")
        self.assertEqual(g_config.MANIFEST_REPO, "Org/inst")

    def test_legacy_gcore_is_found_alone(self):
        g = self._dir("gcore", origin="https://github.com/Org/old.git")
        self.assertEqual(g_config._instance(), g)
        self.assertEqual(g_config.MANIFEST_PATH, g / "manifest.json")
        self.assertEqual(g_config.MANIFEST_REPO, "Org/old")

    def test_env_wins_over_the_dirs_beside_the_repo(self):
        self._dir("gp-core", origin="https://github.com/Org/inst.git")
        other = self._dir("elsewhere", origin="git@github.com:Other/inst")
        os.environ["G_INSTANCE"] = str(other)
        self.assertEqual(g_config.MANIFEST_PATH, other / "manifest.json")
        self.assertEqual(g_config.MANIFEST_REPO, "Other/inst")
        os.environ["G_INSTANCE_REPO"] = "Set/explicitly"
        self.assertEqual(g_config.MANIFEST_REPO, "Set/explicitly")
        os.environ["GCORE_DIR"] = str(self.tmp / "legacy")
        self.assertEqual(g_config._instance(), self.tmp / "legacy")
        os.environ.update(GCORE_MANIFEST="/m/manifest.json", GCORE_ENV="/p/.env")
        self.assertEqual(g_config.MANIFEST_PATH, Path("/m/manifest.json"))
        self.assertEqual(g_config._instance("env"), Path("/p/.env"))

    def test_origin_url_forms_reduce_to_owner_and_name(self):
        for url, want in (("https://user:secret@github.com/Org/inst.git", "Org/inst"),
                          ("git@github.com:Org/inst.git", "Org/inst"),
                          ("ssh://git@github.com/Org/inst/", "Org/inst"),
                          ("inst", "")):
            d = self._dir(f"i{len(os.listdir(self.tmp))}", origin=url)
            os.environ["G_INSTANCE"] = str(d)
            self.assertEqual(g_config.MANIFEST_REPO, want, url)
            self.assertNotIn("secret", g_config.MANIFEST_REPO)

    def test_instance_without_an_origin_names_no_repo(self):
        d = self._dir("gp-core")
        (d / ".git" / "config").write_text('[remote "upstream"]\n\turl = https://github.com/Org/x.git\n')
        self.assertEqual(g_config._instance(), d)
        self.assertEqual(g_config.MANIFEST_REPO, "")

    def test_no_instance(self):
        self.assertIsNone(g_config._instance())
        self.assertIsNone(g_config._instance("env"))
        self.assertEqual(g_config.MANIFEST_PATH, self.tmp / "gp-core" / "manifest.json")
        self.assertFalse(g_config.MANIFEST_PATH.exists())
        self.assertEqual(g_config.MANIFEST_REPO, "")
        self.assertEqual(g_config.BRAIN_HOST, "")


class EnvChain(unittest.TestCase):
    def test_repo_root_and_chain_match_the_spine(self):
        self.assertEqual(g_config.REPO_ROOT, REPO)
        passport = g_config._instance("env")
        self.assertEqual(g_config._ENV_FILES, [REPO / ".env", REPO / "voice" / ".env",
                                               *([passport] if passport else [])])
        self.assertIs(brain_alerts.env, g_config.env)
        self.assertIs(brain_alerts._env, g_config.env)

    def test_env_resolves_like_the_spine(self):
        with tempfile.TemporaryDirectory() as d:
            first, second = Path(d) / "a.env", Path(d) / "b.env"
            first.write_text("ONLY_A=1\nBOTH='from-a'\n")
            second.write_text('ONLY_B="2"\nBOTH=from-b\n')
            files = [first, Path(d) / "missing.env", second]
            with patch.object(g_config, "_ENV_FILES", files), \
                    patch.dict(os.environ, {"LIVE_KEY": "live", "ONLY_A": ""}):
                for key, want in (("LIVE_KEY", "live"), ("ONLY_A", "1"), ("ONLY_B", "2"),
                                  ("BOTH", "from-a"), ("ABSENT", "")):
                    self.assertEqual(g_config.env(key), want, key)
                    self.assertEqual(brain_alerts._env(key), want, key)


if __name__ == "__main__":
    unittest.main()
