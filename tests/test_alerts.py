"""Spine ntfy request shape: default headers unchanged, optional Click URL passed through."""
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
                (brain_alerts, "dry_run", lambda: False),
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


if __name__ == "__main__":
    unittest.main()
