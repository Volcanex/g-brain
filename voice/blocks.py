"""Reply blocks: the structured part of an app-session reply.

A reply to the G-Command apps is `{"reply": text, "blocks": [Block]}`. The block
shapes are g-command-web's panel contract (core/panels/CLAUDE.md, "Blocks"), the
one schema the web, the iPhone and the Mac all render. This module only checks
what the model hands over against that schema and drops what does not fit, so a
client never receives a block it cannot draw. It invents no shapes of its own:
a new block type is added to the spec first, then to `TYPES` here.

`panel` is a reference, not content: `{"type": "panel", "name": "calendar"}`.
g-command-web's proxy expands it into the live panel built by the same code as
the dash, so "what's on today" shows exactly the dash's event rows.
"""
import re

TONES = {"ok", "warn", "danger", "accent", "muted", "plain",
         "chart-1", "chart-2", "chart-3", "chart-4", "chart-5", "chart-6"}
PANELS = ("calendar", "tasks", "money", "brief", "health", "compute", "tickets", "crons")
TYPES = ("rows", "line", "meter", "bullets", "detail", "image", "fold", "panel")

MAX_BLOCKS = 12
MAX_ROWS = 30
MAX_ITEMS = 20
TEXT = 600       # one row title, chip or bullet
LONG = 2000      # a detail paragraph
# Images in a reply are only ever files g-command-web serves behind its own
# auth (uploads, /api/voice/files/<id>). An outside URL would make the app
# fetch from wherever the model pointed it.
IMAGE_URL = re.compile(r"^/api/voice/files/[0-9a-f]{32}$")
LINK = re.compile(r"^/[A-Za-z0-9/_\-.?=&%#:+~]*$")


def _text(v, limit=TEXT):
    return v.strip()[:limit] if isinstance(v, str) and v.strip() else None


def _link(v):
    return v if isinstance(v, str) and LINK.match(v) and not v.startswith("//") else None


def _tone(v):
    return v if v in TONES else None


def chip(c):
    if isinstance(c, str):
        c = {"text": c}
    if not isinstance(c, dict):
        return None
    out = {"text": _text(c.get("text"), 120) or ""}
    if not out["text"] and not _text(c.get("icon"), 40):
        return None
    for key, fn in (("tone", _tone), ("icon", lambda v: _text(v, 40)), ("tip", lambda v: _text(v, 300))):
        val = fn(c.get(key))
        if val:
            out[key] = val
    for key in ("badge", "end"):
        if c.get(key) is True:
            out[key] = True
    return out


def _chips(items):
    return [x for x in (chip(c) for c in (items or [])[:MAX_ITEMS]) if x] if isinstance(items, list) else []


# The only actions a reply row may carry, and only on rows Voice's own code
# built (todos.py, via clean(actions=True)), never on rows the model wrote.
ACTION_PATH = re.compile(r"^/api/brain/api/todos/[A-Za-z0-9~!*'()%_.\-]+$")


def action(a):
    if not (isinstance(a, dict) and a.get("method") in ("PATCH", "DELETE")
            and isinstance(a.get("path"), str) and ACTION_PATH.match(a["path"]) and _text(a.get("label"), 40)):
        return None
    out = {"label": _text(a["label"], 40), "method": a["method"], "path": a["path"]}
    if isinstance(a.get("body"), dict):
        out["body"] = a["body"]
    if isinstance(a.get("error"), dict):
        out["error"] = {k: _text(a["error"].get(k), 300) for k in ("title", "desc") if _text(a["error"].get(k), 300)}
    return out


def row(r, i, actions=False):
    if isinstance(r, str):
        r = {"title": r}
    if not isinstance(r, dict) or not _text(r.get("title")):
        return None
    out = {"id": _text(r.get("id"), 80) or f"r{i}", "title": _text(r["title"])}
    if _text(r.get("detail")):
        out["detail"] = _text(r["detail"])
    for key in ("lead", "trail"):
        c = chip(r.get(key)) if r.get(key) is not None else None
        if c:
            out[key] = c
    if _link(r.get("link")):
        out["link"] = r["link"]
    for key in ("done", "struck"):
        if r.get(key) is True:
            out[key] = True
    for key in ("accent", "title_tone"):
        if _tone(r.get(key)):
            out[key] = r[key]
    marks = _chips(r.get("marks"))
    if marks:
        out["marks"] = marks
    # No `action` on rows the model wrote: a reply's rows are a snapshot, and
    # ticks belong to live panels. todos.py's rows are server-built and may.
    if actions and action(r.get("action")):
        out["action"] = action(r["action"])
    return out


def _block(b, depth, actions=False):
    if not isinstance(b, dict):
        return None
    t = b.get("type")
    if t == "rows":
        rows = [x for x in (row(r, i, actions) for i, r in enumerate((b.get("rows") or [])[:MAX_ROWS])) if x] \
            if isinstance(b.get("rows"), list) else []
        out = {"type": "rows", "rows": rows}
        if _text(b.get("title"), 120):
            out["title"] = _text(b["title"], 120)
        if _text(b.get("empty"), 120):
            out["empty"] = _text(b["empty"], 120)
        if isinstance(b.get("count"), int) and not isinstance(b.get("count"), bool):
            out["count"] = b["count"]
        return out if rows or "empty" in out else None
    if t == "line":
        items = _chips(b.get("items"))
        return {"type": "line", "items": items} if items else None
    if t == "meter":
        label = _text(b.get("label"), 80)
        try:
            value, mx = float(b.get("value")), float(b.get("max", 100))
        except (TypeError, ValueError):
            return None
        if not label or mx <= 0:
            return None
        out = {"type": "meter", "label": label, "value": value, "max": mx}
        for key in ("warn_at", "danger_at"):
            if isinstance(b.get(key), (int, float)) and not isinstance(b.get(key), bool):
                out[key] = b[key]
        if _text(b.get("text"), 160):
            out["text"] = _text(b["text"], 160)
        if _tone(b.get("tone")):
            out["tone"] = b["tone"]
        return out
    if t == "bullets":
        items = []
        for it in (b.get("items") or [])[:MAX_ITEMS] if isinstance(b.get("items"), list) else []:
            it = {"text": it} if isinstance(it, str) else it
            if isinstance(it, dict) and _text(it.get("text")):
                x = {"text": _text(it["text"])}
                if _text(it.get("label"), 40):
                    x["label"] = _text(it["label"], 40)
                if _link(it.get("link")):
                    x["link"] = it["link"]
                items.append(x)
        return {"type": "bullets", "items": items} if items else None
    if t == "detail":
        text = _text(b.get("text"), LONG)
        return {"type": "detail", "text": text} if text else None
    if t == "image":
        url = b.get("url")
        if not (isinstance(url, str) and IMAGE_URL.match(url)):
            return None
        out = {"type": "image", "url": url}
        if _text(b.get("alt"), 200):
            out["alt"] = _text(b["alt"], 200)
        for key in ("width", "height"):
            if isinstance(b.get(key), int) and 0 < b[key] <= 20000:
                out[key] = b[key]
        return out
    if t == "fold":
        title = _text(b.get("title"), 120)
        if not title or depth > 0:   # one level: a fold never holds a fold
            return None
        inner = clean(b.get("blocks"), depth + 1)
        if not inner:
            return None
        return {"type": "fold", "title": title, "open": b.get("open") is True, "blocks": inner}
    if t == "panel":
        return {"type": "panel", "name": b["name"]} if b.get("name") in PANELS else None
    return None


def clean(blocks, depth=0, actions=False):
    """The blocks that fit the contract, in order, at most MAX_BLOCKS."""
    if not isinstance(blocks, list):
        return []
    out = []
    for b in blocks:
        x = _block(b, depth, actions)
        if x:
            out.append(x)
        if len(out) >= MAX_BLOCKS:
            break
    return out


def problems(blocks):
    """One line per block that was dropped, for the model to fix its call."""
    if not isinstance(blocks, list):
        return ["blocks must be a list"]
    issues = []
    for i, b in enumerate(blocks):
        if _block(b, 0) is None:
            t = b.get("type") if isinstance(b, dict) else None
            issues.append(f"block {i} ({t or 'no type'}) dropped: "
                          + ("unknown type" if t not in TYPES else "missing or invalid fields"))
    return issues
