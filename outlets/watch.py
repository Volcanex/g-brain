"""Event-driven calendar mirror: syncs brain->calendar shortly after a real todo
change. Hardened against churn: it only syncs when the todos actually changed
(signature check), and never more than once every MIN_INTERVAL seconds. The
outlet-calendar.timer (every 5 min) remains the full reconcile and the channel
that catches calendar->brain deletions.

Decoupled: the brain writes todo files obliviously; the outlet watches them.
"""
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import brain_alerts as alerts
import brain_todos
import g_config
from outlets import gcal

TODOS = str(g_config.TODOS_DIR)
MIN_INTERVAL = 60  # seconds, floor between syncs, so churn can't hammer Google

_dirty = threading.Event()
_last_sig = [None]

# Fields that actually affect the calendar projection.
_KEYS = ("permalink", "title", "status", "due", "start", "end",
         "all_day", "rrule", "location", "category", "remind_min")


def _signature():
    items = brain_todos.list_todos(status="open")
    return str(sorted(tuple(str(t.get(k)) for k in _KEYS) for t in items))


def _run(force=False):
    try:
        sig = _signature()
    except Exception as e:
        # A failed read must NOT look like "no todos" and skip the sync silently.
        print("watch: reading todos failed:", repr(e), flush=True)
        alerts.send("error", "calendar-sync", f"reading todos failed: {e!r}",
                    dedup_key="calendar-sync")
        return
    if not force and sig == _last_sig[0]:
        return  # nothing relevant changed, skip the Google round-trip
    _last_sig[0] = sig
    try:
        print("watch: synced ->", gcal.sync_and_alert(), flush=True)
    except Exception as e:
        print("watch: sync failed:", repr(e), flush=True)


class _Handler(FileSystemEventHandler):
    def on_any_event(self, event):
        _dirty.set()


def _worker():
    _run(force=True)  # reconcile once on startup
    while True:
        _dirty.wait()
        _dirty.clear()
        _run()
        time.sleep(MIN_INTERVAL)  # cooldown: coalesce churn during this window


def main():
    obs = Observer()
    obs.schedule(_Handler(), TODOS, recursive=True)
    obs.start()
    print("watch: watching", TODOS, flush=True)
    threading.Thread(target=_worker, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        obs.stop()
    obs.join()


if __name__ == "__main__":
    main()
