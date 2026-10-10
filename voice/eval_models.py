"""Measure a candidate VOICE_MODEL against the real toolset, before switching.

From the repo root:

    set -a && . voice/.env && set +a
    .venv/bin/python -m voice.eval_models deepseek/deepseek-v4.1-flash [more...]

Why this exists
---------------
The model is chosen on one property: does it actually CALL create_todo when
the owner states something dated, in the phrasing people really use. A model can be
articulate and still answer "Got it, noted." having written nothing, which is
the failure that cost a week of missed reminders in Sep 2026.

Two traps this harness exists to avoid, both of which produced a confidently
wrong answer the first time:

1. **Judging only the first assistant turn.** A model that calls brain_search
   before writing looks like a failure on turn one and is behaving correctly --
   it commits on turn two or three. Scored that way, the ranking inverted. So
   run the FULL loop, exactly as agent.handle does, and ask only whether
   create_todo was ever called with a date.
2. **Testing cold only.** The production misses happened mid-conversation,
   after a large JSON tool dump, not on a clean first turn. Every case runs
   twice: cold, and with prior history.

Tool results are stubbed, so this writes nothing to the brain.
"""
import concurrent.futures as cf
import json
import os
import random
import sys
import time

import httpx

from . import config
from .agent import TOOLS
from .prompt import SYSTEM

URL = config.OPENROUTER_URL
TODAY = "2026-09-13"          # a Sunday-before-Monday week, so relative dates bite
MAX_ITERS = 6
RUNS = int(os.environ.get("EVAL_RUNS", "3"))

SYS = (SYSTEM + f"\n\nToday's date is {TODAY}. When a date is given without a year "
       "or month, assume the nearest FUTURE occurrence - never schedule in the past. "
       "Always store dates as YYYY-MM-DD.\n"
       "\n## Right now (your short-term memory)\n(pulse omitted for eval)\n")

STUB = {
    "brain_search": [], "brain_read": {"error": "no note"}, "recent_activity": [],
    "list_todos": [], "create_todo": {"ok": True, "permalink": "brain/todos/stub"},
    "pulse_note": {"ok": True}, "pulse_page": {"page": "(stub)"},
    "propose_commit": {"plan_id": "stub"}, "execute_commit": {"ok": True},
}

# The state the real failures happened in: a big tool dump still in history.
WARM = [
    {"role": "user", "content": "When is my haircut"},
    {"role": "assistant", "content": json.dumps(
        [{"title": "Haircut", "due": "2026-09-15", "status": "open",
          "body": "x" * 200} for _ in range(6)])},
    {"role": "user", "content": "ok thanks"},
    {"role": "assistant", "content": "No problem."},
]


def _dated(made):
    return any(n == "create_todo" and (a.get("due") or a.get("start")) for n, a in made)


def _timed(made):
    return any(n == "create_todo" and "T" in str(a.get("start") or "") for n, a in made)


def _no_todo(made):
    return not any(n == "create_todo" for n, _ in made)


# Phrased the way real messages arrive (lower case, a weekday for a date, the time
# left implicit), plus negative controls that must NOT create a todo.
CASES = [
    ("birthday-casual", "Sam birthday this Sunday", _dated),
    ("parcel-monday", "Set reminder to post the parcel for Monday at 15:00", _timed),
    ("chore-time", "Do a wash at 15:00 today", _timed),
    ("optician", "optician thursday 9am", _timed),
    ("recycling", "put the recycling out tomorrow night", _dated),
    ("casual-remind", "remind me to call the plumber friday", _dated),
    ("implicit", "got a bus to catch at 7:40 on the 20th", _timed),
    ("question", "when is my next haircut?", _no_todo),
    ("undated-fact", "I'm leaning towards the green sofa for now", _no_todo),
]


def _post(body, tries=4):
    last = ""
    for i in range(tries):
        try:
            with httpx.Client(timeout=120) as cl:
                r = cl.post(URL, headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                                          "X-Title": "voice-model-eval"}, json=body)
            if r.status_code == 200:
                return r.json()["choices"][0]["message"], ""
            last = f"http {r.status_code}"
            if r.status_code in (429, 502, 503):
                time.sleep(2 * (i + 1) + random.random() * 2)
                continue
            return None, last
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
            time.sleep(1 + i)
    return None, last


def run_loop(model, msg, warm):
    """Mirror agent.handle's loop: append the assistant message, feed tool
    results back. Returns every (tool_name, args) pair the model produced."""
    messages = [{"role": "system", "content": SYS}] + (WARM if warm else []) + \
               [{"role": "user", "content": msg}]
    made = []
    for _ in range(MAX_ITERS):
        m, err = _post({"model": model, "tools": TOOLS, "tool_choice": "auto",
                        "messages": messages})
        if m is None:
            return made, err
        messages.append(m)
        calls = m.get("tool_calls") or []
        if not calls:
            return made, ""
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            made.append((name, args))
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": json.dumps(STUB.get(name, {}))})
    return made, "(iteration cap)"


def score(model):
    jobs = [(lab, msg, pred, warm)
            for (lab, msg, pred) in CASES for warm in (False, True) for _ in range(RUNS)]

    def job(j):
        lab, msg, pred, warm = j
        made, err = run_loop(model, msg, warm)
        note = err or ("+".join(n for n, _ in made) or "NO-CALL")
        return lab, warm, (pred(made) and not err), note, len(made)

    out = {}
    depths = []
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for lab, warm, ok, note, depth in ex.map(job, jobs):
            rec = out.setdefault((lab, warm), [0, 0, []])
            rec[1] += 1
            depths.append(depth)
            if ok:
                rec[0] += 1
            else:
                rec[2].append(note)
    return out, depths


def main(models):
    if not config.OPENROUTER_API_KEY:
        sys.exit("OPENROUTER_API_KEY not set: source voice/.env first")
    for model in models:
        res, depths = score(model)
        cold = sum(v[0] for k, v in res.items() if not k[1])
        cold_n = sum(v[1] for k, v in res.items() if not k[1])
        warm = sum(v[0] for k, v in res.items() if k[1])
        warm_n = sum(v[1] for k, v in res.items() if k[1])
        print(f"\n{model}")
        print(f"   cold {cold}/{cold_n}   warm {warm}/{warm_n}   "
              f"TOTAL {cold + warm}/{cold_n + warm_n}   max chain {max(depths)}")
        for (lab, w), (ok, n, notes) in sorted(res.items()):
            if ok < n:
                print(f"      MISS {lab:16} {'warm' if w else 'cold'} {ok}/{n}  {notes[:2]}")
        # A long chain is a warning even when every case passes: exceeding
        # MAX_TOOL_ITERATIONS returns the "got stuck" fallback and writes nothing.
        if max(depths) >= config.MAX_TOOL_ITERATIONS:
            print(f"      WARNING chain reached {max(depths)} vs "
                  f"MAX_TOOL_ITERATIONS={config.MAX_TOOL_ITERATIONS}, raise the cap")


if __name__ == "__main__":
    main(sys.argv[1:] or [config.MODEL])
