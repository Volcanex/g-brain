"""brain-capture: the miner's way into the brain without any MCP connector.

A headless job on any vendor can only use files and Bash, so the two things the
brain MCP gave the miner are plain commands here:

    python -m brain_capture search "<query>" [...]   rank existing notes by overlap
    python -m brain_capture apply < plan.json         write notes (actor ai-auto) + a pulse line

plan.json: {"notes": [{"text": "...", "layer": "design", "bar": "why this matters and exists nowhere else",
            "title": "...", "target": "designs/x.md", "summary": "...", "tags": []}],
            "pulse": "one currently-working-on line or empty"}
`target` thickens an existing note (brain_note appends instead of colliding). Writes go
through brain_commit.brain_note, so lineage, the commit log and index rebuild are unchanged;
only `actor` is forced to ai-auto. Exit 1 if any write failed.
"""
import json
import re
import sys

import g_config

BRAIN_ROOT = g_config.BRAIN_ROOT
DIRS = ("designs", "ideas", "about", "hazards")
KIND_LAYERS = {"design": "design", "idea": "idea", "fact": "about", "hazard": "hazard"}
WORD = re.compile(r"[a-z0-9][a-z0-9_.-]{3,}")


def _words(text):
    return set(WORD.findall(text.lower()))


def _note_fields(path):
    raw = path.read_text(errors="replace")
    head, _, body = raw.partition("\n---\n") if raw.startswith("---\n") else ("", "", raw)
    def fm(key):
        m = re.search(rf"^{key}:\s*(.+)$", head, re.M)
        return m.group(1).strip().strip("'\"") if m else ""
    return fm("title") or path.stem, fm("summary"), body[:2000]


def search(query, limit=6):
    """Rank notes by rare-word overlap (idf-weighted), titles and summaries counting more."""
    import math
    docs = []
    for d in DIRS:
        for path in (BRAIN_ROOT / d).rglob("*.md"):
            if ".history" in path.parts:
                continue
            title, summary, body = _note_fields(path)
            docs.append((str(path.relative_to(BRAIN_ROOT)), title, summary, _words(title), _words(summary), _words(body)))
    df = {}
    for doc in docs:
        for w in doc[3] | doc[4] | doc[5]:
            df[w] = df.get(w, 0) + 1
    idf = lambda w: math.log(1 + len(docs) / df.get(w, 1))
    q = _words(query)
    hits = []
    for rel, title, summary, tw, sw, bw in docs:
        score = sum(idf(w) * (3 * (w in tw) + 2 * (w in sw) + (w in bw)) for w in q if w in tw | sw | bw)
        if score:
            hits.append((round(score, 1), rel, title, summary))
    return sorted(hits, reverse=True)[:limit]


def apply(plan):
    from brain_commit import brain_note
    results = []
    for note in plan.get("notes", []):
        title = note.get("title") or note["text"][:40]
        layer = note.get("layer") or KIND_LAYERS.get(note.get("kind"))
        if layer not in KIND_LAYERS.values():
            results.append({"ok": True, "action": "skipped", "title": title,
                            "reason": "No clear layer mapping. Use design, idea, about or hazard."})
            continue
        out = brain_note(text=note["text"], layer=layer, bar=note.get("bar"), target=note.get("target"),
                         title=note.get("title"), tags=note.get("tags"), summary=note.get("summary"),
                         actor="ai-auto")
        results.append({"title": title, **{k: out[k] for k in
                        ("ok", "action", "target", "error", "near_duplicates", "hint") if k in out}})
    pulse = (plan.get("pulse") or "").strip()
    if pulse:
        import brain_pulse
        page = brain_pulse.PAGE_PATH.read_text() if brain_pulse.PAGE_PATH.exists() else ""
        if pulse not in page:
            brain_pulse.append_page(pulse)
            results.append({"pulse": "added"})
    return results


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["search"] and len(argv) > 1:
        for query in argv[1:]:
            print(f"## {query}")
            for score, rel, title, summary in search(query):
                print(f"{score:>5} {rel} | {title} | {summary[:160]}")
        return 0
    if argv[:1] == ["apply"]:
        try:
            results = apply(json.load(sys.stdin))
        except Exception as e:  # noqa: BLE001 - the model needs the reason
            print(f"FAILED: {e!r}")
            return 1
        print(json.dumps(results))
        bad = [r for r in results if r.get("ok") is False]
        print("FAILED: " + json.dumps(bad) if bad else "applied")
        return 1 if bad else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
