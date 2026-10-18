"""brain_miner and the pulse record CLI, with a fake g-agent-run: no tokens are spent."""
import json
import os
import stat
import sys

import pytest

import brain_miner

FAKE = """#!/bin/sh
cat >/dev/null
printf '%s\\n' "$@" > "$ARGV_OUT"
printf 'thinking\\n%s\\n' "$FAKE_LAST"
exit "${FAKE_EXIT:-0}"
"""


@pytest.fixture
def fake_runner(tmp_path, monkeypatch):
    path = tmp_path / "g-agent-run"
    path.write_text(FAKE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(brain_miner, "AGENT_RUN", str(path))
    monkeypatch.setenv("ARGV_OUT", str(tmp_path / "argv.txt"))
    return tmp_path


def _transcript(tmp_path):
    p = tmp_path / "s.jsonl"
    line = json.dumps({"type": "user", "message": {"role": "user", "content": "x" * 1400}})
    p.write_text((line + "\n") * 3)
    return str(p)


@pytest.mark.parametrize("last,exit_code,expect", [
    ("RESULT: ok - MINED n_notes=1 pulse=0", "0", "exit=0 ok"),
    ("RESULT: noop - nothing durable", "0", "exit=0 noop"),
    ("RESULT: fail - Brain MCP unreachable", "0", "exit=1 fail"),
    ("no result line here", "0", "exit=1 no RESULT"),
    ("RESULT: ok - x", "3", "exit=3"),
])
def test_mine_goes_through_runner_and_reads_result(fake_runner, monkeypatch, last, exit_code, expect):
    monkeypatch.setenv("FAKE_LAST", last)
    monkeypatch.setenv("FAKE_EXIT", exit_code)
    out = brain_miner._mine("h", _transcript(fake_runner), 0, False)
    assert out.startswith(expect)
    argv = (fake_runner / "argv.txt").read_text().split()
    assert argv[argv.index("--role") + 1] == "verify"
    assert "--tools" in argv and "--connectors" not in argv  # no vendor-specific coupling
    assert argv[argv.index("--sandbox") + 1] == "danger-full-access"


def test_pulse_record_cli_writes_state(tmp_path, monkeypatch):
    import brain_pulse
    inlets, state = tmp_path / "inlets", tmp_path / "state"
    inlets.mkdir()
    (inlets / "demo.md").write_text("---\nid: demo\n---\nbody\n")
    monkeypatch.setattr(brain_pulse, "INLETS_DIR", inlets)
    monkeypatch.setattr(brain_pulse, "STATE_DIR", state)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("two replies owed"))
    assert brain_pulse._cli(["record", "demo", "--summary", "s"]) == 0
    assert "two replies owed" in (state / "demo.md").read_text()
    assert brain_pulse._cli(["record", "nope"]) == 1


def test_subagent_transcripts_are_never_eligible(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / "sess" / "subagents").mkdir(parents=True)
    big = "x" * 20_000
    for p in (root / "main.jsonl", root / "sess" / "subagents" / "agent-1.jsonl"):
        p.write_text(big)
        os.utime(p, (1, 1))
    monkeypatch.setattr(brain_miner, "SOURCES", [("h", str(root))])
    monkeypatch.setattr(brain_miner, "LEDGER", str(tmp_path / "ledger.json"))
    monkeypatch.setattr(brain_miner.time, "time", lambda: 1 + brain_miner.QUIET_S + 100)
    seen = []
    monkeypatch.setattr(brain_miner, "_mine", lambda host, p, m, dry: seen.append(p) or "skipped-thin")
    monkeypatch.setattr(sys, "argv", ["brain_miner", "--dry-run"])
    brain_miner.main()
    assert [os.path.basename(p) for p in seen] == ["main.jsonl"]


def test_distil_drops_system_reminders_and_caps_assistant_text():
    import tempfile
    rows = [{"type": "user", "message": {"role": "user", "content": "decide <system-reminder>NOISE</system-reminder> the thing"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "y" * 3000}]}}]
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        f.write("\n".join(json.dumps(r) for r in rows))
    out = brain_miner._distil(f.name)
    assert "NOISE" not in out and "decide the thing" in out
    assert out.count("y") == brain_miner.ASSISTANT_MSG_CAP


def test_capture_apply_forces_ai_auto_and_fails_loudly(monkeypatch):
    import brain_capture, brain_commit
    seen = []
    def fake(**kw):
        seen.append(kw)
        return {"ok": kw["title"] != "bad", "action": "created", "target": "hazards/x.md", "error": "nope"}
    monkeypatch.setattr(brain_commit, "brain_note", fake)
    out = brain_capture.apply({"notes": [
        {"text": "t", "kind": "hazard", "title": "good", "bar": "Saves a repeat failure, recorded nowhere else."},
        {"text": "t", "layer": "about", "title": "bad", "bar": "Preserves a preference absent elsewhere."}]})
    assert [k["actor"] for k in seen] == ["ai-auto", "ai-auto"]
    assert [r["ok"] for r in out] == [True, False]

    assert [k["layer"] for k in seen] == ["hazard", "about"]
    assert all(k["bar"] for k in seen)
    assert all("kind" not in k for k in seen)
