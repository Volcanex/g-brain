"""Vital dependency daily re-page: first page, quiet <24h, re-page at 24h, recovery."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import brain_alerts
import brain_status as status

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def dep(state, detail="balance exhausted", crit="vital"):
    return {"id": "openrouter", "kind": "dependency", "criticality": crit, "state": state,
            "detail": detail, "powers": "all LLM inference: Voice, pulse crons, every agent"}


class VitalRepage(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        # dry_run pinned off: an inherited BRAIN_ALERTS_DRY_RUN=1 must not change the result.
        for name, val in (("STATE_DIR", d), ("ALERTS_STATE", d / "alerts.json"),
                          ("dry_run", lambda: False)):
            p = patch.object(brain_alerts, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.sent = []

        def fake_send(level, source, msg, dedup_key=None):
            self.sent.append((level, msg))
            if dedup_key:
                brain_alerts._stamp(dedup_key)
            return {"ok": True}
        p = patch.object(brain_alerts, "send", fake_send)
        p.start()
        self.addCleanup(p.stop)
        self.clock = T0

    def tick(self, prior, now_state, at=None, **kw):
        at = at or self.clock
        with patch.object(status, "_now", lambda: at):
            status._alert_transitions({"openrouter": prior}, [dep(now_state, **kw)])

    def test_first_page_includes_powers(self):
        self.tick("up", "down")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "error")
        self.assertIn("powers all LLM inference", self.sent[0][1])

    def test_no_repage_before_24h(self):
        self.tick("up", "down")
        self.tick("down", "down", at=T0 + timedelta(hours=23, minutes=55))
        self.assertEqual(len(self.sent), 1)

    def test_repage_at_24h_then_clock_advances(self):
        self.tick("up", "down")
        self.tick("down", "down", at=T0 + timedelta(hours=24))
        self.assertEqual(len(self.sent), 2)
        msg = self.sent[1][1]
        self.assertIn("STILL DOWN for 1d 0h", msg)
        self.assertIn("all LLM inference", msg)
        self.tick("down", "down", at=T0 + timedelta(hours=30))
        self.assertEqual(len(self.sent), 2)
        self.tick("down", "down", at=T0 + timedelta(hours=48))
        self.assertEqual(len(self.sent), 3)
        self.assertIn("2d 0h", self.sent[2][1])

    def test_state_survives_restart(self):
        self.tick("up", "down")
        # a "restart" is just a fresh call reading the same state file
        self.tick("down", "down", at=T0 + timedelta(hours=1))
        self.assertEqual(len(self.sent), 1)

    def test_recovery_resets_clock(self):
        self.tick("up", "down")
        self.tick("down", "up", at=T0 + timedelta(hours=5), detail="ok")
        self.assertEqual(len(self.sent), 2)
        self.assertIn("recovered", self.sent[1][1])
        state = brain_alerts._load_state()
        self.assertFalse([k for k in state if k.endswith(("down_since", "last_page"))])
        # new outage later: fresh first page, and no re-page 23h in
        t1 = T0 + timedelta(days=3)
        self.tick("up", "down", at=t1)
        self.assertEqual(len(self.sent), 3)
        self.tick("down", "down", at=t1 + timedelta(hours=23))
        self.assertEqual(len(self.sent), 3)

    def test_non_vital_never_repages(self):
        self.tick("up", "down", crit="important")
        self.tick("down", "down", at=T0 + timedelta(days=3), crit="important")
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
