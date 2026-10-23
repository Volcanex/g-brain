"""Spine ntfy request shape: default headers unchanged, optional Click URL passed through.
And the app push fan-out: each destination fails alone, dry-run and cooldown gate both."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import brain_alerts


class NtfyHeaders(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        self.requests = []
        for target, name, val in (
                (brain_alerts, "STATE_DIR", d), (brain_alerts, "ALERTS_STATE", d / "alerts.json"),
                (brain_alerts, "dry_run", lambda: False), (brain_alerts, "_push", lambda *a: None),
                (brain_alerts.urllib.request, "urlopen", lambda req, timeout: self.requests.append(req))):
            p = patch.object(target, name, val)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"NTFY_URL": "http://ntfy.invalid/topic"})
        p.start()
        self.addCleanup(p.stop)

    def headers(self):
        self.assertEqual(len(self.requests), 1)
        return dict(self.requests[0].header_items())

    def test_default_headers_have_no_click(self):
        self.assertEqual(brain_alerts.send("warn", "calendar-sync", "down"), {"ok": True})
        self.assertEqual(self.headers(), {"Title": "calendar-sync", "Priority": "high", "Tags": "warning"})
        self.assertEqual(self.requests[0].full_url, "http://ntfy.invalid/topic")
        self.assertEqual(self.requests[0].data, b"down")

    def test_click_url_becomes_the_click_header(self):
        brain_alerts.send("info", "brief", "morning", click="https://command.example/pulse")
        self.assertEqual(self.headers(), {"Title": "brief", "Priority": "default",
                                          "Click": "https://command.example/pulse"})

    def test_cli_passes_click(self):
        with patch("sys.stdout"):
            self.assertEqual(brain_alerts._cli(["--level", "error", "--source", "cli",
                                                "--click", "https://x.example/a", "boom"]), 0)
        self.assertEqual(self.headers()["Click"], "https://x.example/a")


class FanOut(unittest.TestCase):
    """ntfy and ALERT_PUSH_URL are two destinations that must never take each other down."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        self.sent, self.fail = [], set()
        self.config = {"NTFY_URL": "http://ntfy.invalid/topic", "ALERT_PUSH_URL": "http://push.invalid/api/push",
                       "ALERT_PUSH_TOKEN": "tok"}

        def urlopen(req, timeout):
            dest = "push" if req.full_url.startswith("http://push") else "ntfy"
            if dest in self.fail:
                raise OSError(f"{dest} down")
            self.sent.append((dest, req))

        for target, name, val in (
                (brain_alerts, "STATE_DIR", d), (brain_alerts, "ALERTS_STATE", d / "alerts.json"),
                (brain_alerts, "dry_run", lambda: False),
                (brain_alerts, "env", lambda k: self.config.get(k, "")),
                (brain_alerts.urllib.request, "urlopen", urlopen)):
            p = patch.object(target, name, val)
            p.start()
            self.addCleanup(p.stop)

    def dests(self):
        return [d for d, _ in self.sent]

    def test_both_destinations_get_the_alert(self):
        r = brain_alerts.send("critical", "brain-mcp", "down", click="https://c.example/system")
        self.assertEqual(r, {"ok": True, "push": {"ok": True}})
        self.assertEqual(self.dests(), ["ntfy", "push"])
        push = self.sent[1][1]
        self.assertEqual(push.get_header("Authorization"), "Bearer tok")
        self.assertEqual(json.loads(push.data), {"level": "critical", "source": "brain-mcp", "msg": "down",
                                                 "click": "https://c.example/system"})

    def test_push_failure_leaves_ntfy_and_dedup_alone(self):
        self.fail.add("push")
        r = brain_alerts.send("warn", "x", "y", dedup_key="k")
        self.assertTrue(r["ok"])
        self.assertFalse(r["push"]["ok"])
        self.assertEqual(self.dests(), ["ntfy"])
        self.assertEqual(brain_alerts.send("warn", "x", "y", dedup_key="k"), {"ok": True, "suppressed": True})

    def test_ntfy_failure_still_pushes(self):
        self.fail.add("ntfy")
        r = brain_alerts.send("error", "x", "y", dedup_key="k")
        self.assertFalse(r["ok"])
        self.assertEqual(r["push"], {"ok": True})
        self.assertEqual(self.dests(), ["push"])

    def test_bad_ntfy_url_still_pushes(self):
        self.config["NTFY_URL"] = "not a url"
        r = brain_alerts.send("error", "x", "y")
        self.assertFalse(r["ok"])
        self.assertEqual(self.dests(), ["push"])

    def test_no_ntfy_url_still_pushes(self):
        del self.config["NTFY_URL"]
        r = brain_alerts.send("error", "x", "y")
        self.assertEqual(r["error"], "NTFY_URL unset")
        self.assertEqual(self.dests(), ["push"])

    def test_unset_push_url_is_a_no_op(self):
        del self.config["ALERT_PUSH_URL"]
        self.assertEqual(brain_alerts.send("info", "x", "y"), {"ok": True})
        self.assertEqual(self.dests(), ["ntfy"])

    def test_dry_run_touches_neither(self):
        with patch.object(brain_alerts, "dry_run", lambda: True):
            self.assertEqual(brain_alerts.send("critical", "x", "y"), {"ok": True, "dry_run": True})
        self.assertEqual(self.sent, [])

    def test_cooldown_suppresses_both(self):
        brain_alerts.send("warn", "x", "y", dedup_key="k")
        self.sent.clear()
        brain_alerts.send("warn", "x", "y", dedup_key="k")
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
