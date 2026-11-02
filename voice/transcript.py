"""Persistent per-sender conversation transcript.

Appends one JSON object per event to a JSONL file under
g_config.VOICE_TRANSCRIPT_DIR/<safe_sender>.jsonl (default
$BRAIN_ROOT/.pulse/state/voice).

The directory and files are created on first use. Because the service
runs as the vault user, writes are naturally owned by that user.

Safe filename: keep alphanumerics and '+', strip everything else.
"""
import datetime
import json
import logging
import os
import re
from zoneinfo import ZoneInfo

import g_config

log = logging.getLogger(__name__)

_TRANSCRIPT_DIR = str(g_config.VOICE_TRANSCRIPT_DIR)


def _safe_name(sender: str) -> str:
    return re.sub(r"[^a-zA-Z0-9+]", "", sender) or "unknown"


def _ensure_dir():
    os.makedirs(_TRANSCRIPT_DIR, exist_ok=True)


def _path(sender: str) -> str:
    return os.path.join(_TRANSCRIPT_DIR, f"{_safe_name(sender)}.jsonl")


# Sessions that start fresh at 4am Europe/London each day. The old file is
# archived beside it as <sender>-YYYY-MM-DD.jsonl (the day of its last turn),
# not deleted. Done lazily on read and write so no timer can silently not run.
# g-command-web's /api/voice/history applies the same cutoff when reading.
DAILY_RESET = {"app"}
RESET_TZ = ZoneInfo("Europe/London")
RESET_HOUR = 4


def last_boundary(now=None) -> datetime.datetime:
    """The most recent 4am Europe/London, as an aware UTC datetime."""
    now = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(RESET_TZ)
    cut = now.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    if now < cut:
        cut = (cut - datetime.timedelta(days=1)).replace(hour=RESET_HOUR)   # wall clock, DST safe
    return cut.astimezone(datetime.timezone.utc)


def _last_ts(path: str):
    try:
        with open(path) as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except OSError:
        return None
    for line in reversed(lines):
        try:
            return datetime.datetime.fromisoformat(json.loads(line)["ts"])
        except Exception:
            continue
    return datetime.datetime.fromtimestamp(os.path.getmtime(path), datetime.timezone.utc)


def rotate_if_stale(sender: str, now=None) -> bool:
    """Archive the sender's transcript if its last turn predates the latest 4am
    London boundary. True when it rotated, so the caller drops its in-memory
    context too. Never raises."""
    if sender not in DAILY_RESET:
        return False
    try:
        p = _path(sender)
        if not os.path.exists(p):
            return False
        last = _last_ts(p)
        if last is None:
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=datetime.timezone.utc)
        if last >= last_boundary(now):
            return False
        day = last.astimezone(RESET_TZ).date().isoformat()
        dest = os.path.join(_TRANSCRIPT_DIR, f"{_safe_name(sender)}-{day}.jsonl")
        n = 1
        while os.path.exists(dest):
            n += 1
            dest = os.path.join(_TRANSCRIPT_DIR, f"{_safe_name(sender)}-{day}-{n}.jsonl")
        os.rename(p, dest)
        log.info("rotated %s transcript to %s", sender, os.path.basename(dest))
        return True
    except Exception:
        log.warning("transcript rotate failed for %s", _safe_name(sender), exc_info=True)
        return False


def append(sender: str, role: str, text: str, **extra) -> None:
    """Append one event to the sender's transcript file. Never raises.

    `extra` keys that are not None ride along on the record: the app session
    stores a reply's `blocks` and a user turn's upload `attachments`, so its
    history route can redraw them. load_tail reads only role and text."""
    try:
        _ensure_dir()
        rotate_if_stale(sender)
        rec = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "role": role,
            "text": text,
        }
        rec.update({k: v for k, v in extra.items() if v is not None})
        record = json.dumps(rec)
        with open(_path(sender), "a") as fh:
            fh.write(record + "\n")
    except Exception:
        # Must never crash the voice loop -- but log it. A silent failure here
        # costs the bot its memory across restarts, which looks like the model
        # having gone stupid rather than like a broken write. (This dir was
        # root-owned for two months while the service ran as the vault user.)
        log.warning("transcript append failed for %s", _safe_name(sender), exc_info=True)


def load_tail(sender: str, n: int) -> list:
    """Return the last n events as {"role": ..., "content": ...} dicts
    suitable for direct insertion into a message history list.

    Skips corrupt lines silently. Returns [] if no file exists.
    """
    rotate_if_stale(sender)
    p = _path(sender)
    if not os.path.exists(p):
        return []
    events = []
    try:
        with open(p) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    events.append({"role": obj["role"], "content": obj["text"]})
                except Exception:
                    continue
    except Exception:
        return []
    return events[-n:]
