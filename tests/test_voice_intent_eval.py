"""Paid gate: VOICE_INTENT_EVAL=1 .venv/bin/python -m pytest -q -s tests/test_voice_intent_eval.py.

Each case runs the real app agent and DeepSeek in a fresh process/vault. No Chat
endpoint is called. Write calls count even if refused, deduped or unsuccessful.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


CASES = [
    ("what's on this week?", "read"),
    ("anything due tomorrow?", "read"),
    ("what bills are coming?", "read"),
    ("when's my flight?", "read"),
    ("am I free Friday?", "read"),
    ("what do I owe?", "read"),
    ("remind me what I owe?", "read"),
    ("did I mark the dissertation done?", "read"),
    ("add dentist Thursday 3pm", "dentist"),
    ("move the Excelas call to Friday 10am", "move"),
    ("mark the dissertation done", "done"),
    ("I've got drinks with Rory on the 27th at 7", "drinks"),
    ("I've got a dentist Thursday at 3", "dentist"),
    ("drop the gym class", "drop"),
]


@pytest.mark.skipif(os.environ.get("VOICE_INTENT_EVAL") != "1", reason="paid DeepSeek eval; opt in")
@pytest.mark.parametrize("text,expected", CASES, ids=[c[0] for c in CASES])
def test_deepseek_only_writes_on_request(tmp_path, text, expected):
    env = {**os.environ, "BRAIN_ROOT": str(tmp_path),
           "VOICE_TRANSCRIPT_DIR": str(tmp_path / ".pulse/state/voice"),
           "GCORE_ENV": str(tmp_path / "absent.env"),
           "VOICE_MODEL": "deepseek/deepseek-v4.1-flash"}
    run = subprocess.run([sys.executable, str(Path(__file__).resolve()), text],
                         cwd=Path(__file__).resolve().parents[1], env=env,
                         capture_output=True, text=True, timeout=240)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout.splitlines()[-1])
    print(json.dumps({"prompt": text, "writes": result["writes"], "changed": result["changed"]}))
    writes, items = result["writes"], result["items"]
    if expected == "read":
        assert writes == [] and not result["changed"], result
    else:
        allowed = {"move": {"reschedule_todo", "update_todo"},
                   "done": {"mark_done"}, "drop": {"drop_todo"}}.get(expected, {"create_todo"})
        assert writes and set(writes) <= allowed and result["changed"], result
        if expected in ("dentist", "drinks"):
            added = [i for i in items if expected in i["title"].lower()]
            assert len(added) == 1 and "T" in str(added[0].get("start")), result
            date = result["thursday"] if expected == "dentist" else result["day27"]
            assert str(added[0]["start"]).startswith(date + "T"), result
            assert str(added[0]["start"])[11:16] == ("15:00" if expected == "dentist" else "19:00"), result
        else:
            title = {"move": "Excelas call", "done": "Dissertation", "drop": "Gym class"}[expected]
            item = next(i for i in items if i["title"] == title)
            if expected == "move":
                assert str(item["start"])[:16] == result["friday"] + "T10:00", result
            else:
                assert item["status"] == ("done" if expected == "done" else "cancelled"), result


def _worker(text):
    import datetime as dt
    import tempfile

    root = Path(os.environ["BRAIN_ROOT"]).resolve()
    assert root.is_relative_to(Path(tempfile.gettempdir()).resolve()), "eval requires a temporary vault"
    assert root != Path(tempfile.gettempdir()).resolve(), "eval requires its own temporary directory"
    # Load just the API key; never source the service env and its live paths.
    for line in (Path(__file__).resolve().parents[1] / "voice/.env").read_text().splitlines():
        if line.startswith("OPENROUTER_API_KEY="):
            os.environ["OPENROUTER_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
    assert os.environ.get("OPENROUTER_API_KEY"), "voice env has no OpenRouter key"

    # All brain/voice imports happen after the subprocess environment is isolated.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import brain_todos
    from voice import agent, appchat, todos
    assert brain_todos.TODOS_DIR.resolve() == root / "todos"
    today = todos.today()
    day27 = today.replace(day=27)
    if day27 < today:
        day27 = (today.replace(day=28) + dt.timedelta(days=4)).replace(day=27)

    def day(n):
        return (today + dt.timedelta(days=n)).isoformat()

    for title, fields in [
        ("Tamarix interview", {"start": day(2) + "T11:00:00", "category": "work"}),
        ("Excelas call", {"start": day(1) + "T09:00:00", "category": "work"}),
        ("Dissertation", {"due": day(5), "category": "study"}),
        ("Flight to Stockholm", {"start": day(4) + "T14:00:00", "category": "travel"}),
        ("Electricity bill", {"due": day(3), "category": "finance", "body": "Pay GBP 85."}),
        ("Gym class", {"start": day(3) + "T18:00:00", "category": "health"}),
    ]:
        brain_todos.create(title, "P1", requester="gabriel", composer="agent", source="eval", **fields)
    agent._HISTORY["app"] = [
        {"role": "user", "content": "add the Tamarix interview " + day(2) + " at 11am"},
        {"role": "assistant", "content": "Added the Tamarix interview."},
    ]

    def snapshot():
        return {p.name: p.read_bytes() for p in brain_todos.TODOS_DIR.glob("*.md")}

    before = snapshot()
    done = list(appchat.stream_turn("app", text))[-1]
    items = [brain_todos.serialize(slug, post) for slug, post in brain_todos.iter_todos()]
    print(json.dumps({"writes": [n for n in done["tools"] if n in agent.WRITE_TOOLS],
                      "tools": done["tools"], "changed": snapshot() != before,
                      "items": items, "reply": done["reply"],
                      "thursday": todos.resolve_date("Thursday").isoformat(),
                      "friday": todos.resolve_date("Friday").isoformat(),
                      "day27": day27.isoformat()}, default=str))


if __name__ == "__main__":
    _worker(sys.argv[1])
