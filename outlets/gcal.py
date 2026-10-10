"""outlets/calendar: one-way mirror of brain todos onto Google Calendar.

Todos are the source of truth; the "Brain" calendar is a derived view. Never
reads authority back from the calendar. See designs/outlets + designs/todos-as-l6-commitments.

MVP: each *dated* open todo becomes an all-day event on its due date. Idempotent
via extendedProperties.private.todo_permalink (the event carries its todo's
permalink, so re-runs update in place and orphaned events are removed). Times +
recurrence come when todos gain the iCal-event fields.

Auth: a Google service account (headless, no OAuth flow). Share the Brain
calendar with the service account's client_email. Config from env:
  GOOGLE_SA_B64       base64 of the service-account JSON key
  GOOGLE_CALENDAR_ID  the Brain calendar's id

Run a sync:  .venv/bin/python -m outlets.calendar
"""
import base64
import datetime
import fcntl
import json
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import brain_todos

_STATE = os.path.join(os.path.dirname(__file__), ".synced.json")
_LOCK = os.path.join(os.path.dirname(__file__), ".sync.lock")

SCOPES = ["https://www.googleapis.com/auth/calendar"]
_PROP = "todo_permalink"

# Default reminder on every brain event (unless a todo overrides it): a popup at
# 9am the day before (900 min before the all-day midnight start).
DEFAULT_REMIND_MIN = 900

# Timezone for timed events (the owner's calendar is UK).
TZ = "Europe/London"

# Category -> Google Calendar colorId. Events support 11 colours; one per category.
CATEGORY_COLORS = {
    "work": "9",       # Blueberry  (blue)
    "chore": "8",      # Graphite   (grey)
    "social": "6",     # Tangerine  (orange)
    "family": "4",     # Flamingo   (pink)
    "health": "10",    # Basil      (green)
    "finance": "5",    # Banana     (yellow)
    "study": "3",      # Grape      (purple)
    "deadline": "11",  # Tomato     (red)
    "travel": "7",     # Peacock    (teal)
    "personal": "1",   # Lavender   (soft purple)
    "errand": "2",     # Sage       (light green)
}


def _service():
    raw = base64.b64decode(os.environ["GOOGLE_SA_B64"])
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _plus_hour(dt_str: str) -> str:
    return (datetime.datetime.fromisoformat(dt_str) + datetime.timedelta(hours=1)).isoformat()


def _event_times(t: dict) -> dict:
    """Build the Google start/end blocks from the todo's date fields. Timed when
    `start` carries a time component (has 'T') and not all_day; otherwise all-day
    (dates only; Google's end.date is exclusive, so +1 day)."""
    start = str(t.get("start") or t.get("due"))
    end = t.get("end")
    if "T" in start and not t.get("all_day"):
        e = str(end) if end else _plus_hour(start)
        return {"start": {"dateTime": start, "timeZone": TZ},
                "end": {"dateTime": e, "timeZone": TZ}}
    s_date = start[:10]
    last = str(end)[:10] if end else s_date
    e_date = (datetime.date.fromisoformat(last) + datetime.timedelta(days=1)).isoformat()
    return {"start": {"date": s_date}, "end": {"date": e_date}}


def _existing_event(svc, cal_id: str, permalink: str):
    items = svc.events().list(
        calendarId=cal_id,
        privateExtendedProperty=f"{_PROP}={permalink}",
        showDeleted=False,
    ).execute().get("items", [])
    return items[0] if items else None


def _load_state() -> dict:
    """permalink -> event_id for todos we've synced (outlet-owned bookkeeping)."""
    try:
        with open(_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    with open(_STATE, "w") as f:
        json.dump(state, f)


def _build_body(permalink: str, t: dict) -> dict:
    remind = t.get("remind_min")
    if remind is None:
        remind = DEFAULT_REMIND_MIN
    body = {
        "summary": t.get("title") or "(untitled)",
        "description": ((t.get("summary") or "") + f"\n\nbrain todo: {permalink}").strip(),
        "extendedProperties": {"private": {_PROP: permalink, "source": "brain"}},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": remind}]},
        **_event_times(t),
    }
    cat = (t.get("category") or "").lower()
    if cat in CATEGORY_COLORS:
        body["colorId"] = CATEGORY_COLORS[cat]
    if t.get("rrule"):
        rr = str(t["rrule"])
        body["recurrence"] = [rr if rr.upper().startswith("RRULE:") else "RRULE:" + rr]
    if t.get("location"):
        body["location"] = str(t["location"])
    return body


def _is_gone(svc, cal_id: str, event_id: str) -> bool:
    """Authoritatively check whether an event was deleted on the calendar. Uses a
    direct get on the known id (not a stale property query), so a transient hiccup
    can never be mistaken for a deletion."""
    try:
        ev = svc.events().get(calendarId=cal_id, eventId=event_id).execute()
        return ev.get("status") == "cancelled"
    except HttpError as he:
        return getattr(he, "resp", None) is not None and he.resp.status in (404, 410)


def sync() -> dict:
    """Project dated open todos -> calendar. And the one allowed reverse signal:
    if a brain event was DELETED on the calendar, archive its todo. Everything
    else stays one-way (todos are the source of truth for content).

    Serialized by an exclusive file lock: the continuous watcher and the 5-minute
    timer both call this against one calendar + one state file, and concurrent runs
    race on insert -> duplicate events. The lock makes load-modify-save atomic."""
    with open(_LOCK, "w") as _lf:
        fcntl.flock(_lf, fcntl.LOCK_EX)
        return _sync_locked()


def _sync_locked() -> dict:
    cal_id = os.environ["GOOGLE_CALENDAR_ID"]
    svc = _service()
    state = _load_state()
    new_state: dict = {}

    todos = brain_todos.list_todos(status="open")
    want = {t["permalink"]: t for t in todos if (t.get("start") or t.get("due")) and t.get("permalink")}

    created = updated = removed = archived = 0
    failed: list[str] = []
    for permalink, t in want.items():
        try:
            eid = state.get(permalink)
            if eid:
                if _is_gone(svc, cal_id, eid):
                    # deleted on the calendar -> archive the todo, do not recreate
                    brain_todos.mark_done(permalink)
                    archived += 1
                    continue
                svc.events().update(calendarId=cal_id, eventId=eid, body=_build_body(permalink, t)).execute()
                new_state[permalink] = eid
                updated += 1
            else:
                found = _existing_event(svc, cal_id, permalink)  # migrate / dedupe
                if found:
                    svc.events().update(calendarId=cal_id, eventId=found["id"], body=_build_body(permalink, t)).execute()
                    new_state[permalink] = found["id"]
                    updated += 1
                else:
                    ev = svc.events().insert(calendarId=cal_id, body=_build_body(permalink, t)).execute()
                    new_state[permalink] = ev["id"]
                    created += 1
        except Exception as e:  # one bad todo must never abort the whole sync
            print("sync: todo failed", permalink, repr(e), flush=True)
            failed.append(permalink)
            if permalink in state:
                new_state[permalink] = state[permalink]

    # Cleanup: brain-owned events whose todo is no longer dated-open (done/deleted/un-dated).
    page = None
    while True:
        resp = svc.events().list(calendarId=cal_id, privateExtendedProperty="source=brain", pageToken=page).execute()
        for ev in resp.get("items", []):
            pl = ev.get("extendedProperties", {}).get("private", {}).get(_PROP)
            if not pl:
                continue
            # Delete orphans (todo no longer dated-open) AND duplicates (a second
            # event for a todo we already keep canonically in new_state). The
            # latter self-heals any dupes a past race left behind.
            orphan = pl not in want
            dupe = pl in new_state and ev["id"] != new_state[pl]
            if orphan or dupe:
                try:
                    svc.events().delete(calendarId=cal_id, eventId=ev["id"]).execute()
                    removed += 1
                except Exception as e:
                    print("sync: cleanup failed", pl, repr(e), flush=True)
                    failed.append(pl)
        page = resp.get("nextPageToken")
        if not page:
            break

    _save_state(new_state)
    return {"created": created, "updated": updated, "removed": removed,
            "archived": archived, "dated_todos": len(want), "failed": failed}


def sync_and_alert() -> dict:
    """Run a sync and route any failure to the alert spine, then return the
    result. The whole sync raising (auth, calendar gone) is a hard failure; a
    non-empty `failed` list is a partial failure (some todos didn't reach the
    calendar, exactly the silent miss the spine exists to catch). A clean run
    clears both dedup keys so the next failure alerts immediately."""
    import brain_alerts
    try:
        result = sync()
    except Exception as e:
        brain_alerts.send("error", "calendar-sync", f"sync crashed: {e!r}",
                          dedup_key="calendar-sync")
        raise
    if result.get("failed"):
        brain_alerts.send(
            "error", "calendar-sync",
            f"{len(result['failed'])} todo(s) failed to reach the calendar: "
            + ", ".join(result["failed"]),
            dedup_key="calendar-sync-partial")
    else:
        brain_alerts.clear("calendar-sync")
        brain_alerts.clear("calendar-sync-partial")
    return result


if __name__ == "__main__":
    print(sync_and_alert())
