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

import g_config

log = logging.getLogger(__name__)

_TRANSCRIPT_DIR = str(g_config.VOICE_TRANSCRIPT_DIR)


def _safe_name(sender: str) -> str:
    return re.sub(r"[^a-zA-Z0-9+]", "", sender) or "unknown"


def _ensure_dir():
    os.makedirs(_TRANSCRIPT_DIR, exist_ok=True)


def _path(sender: str) -> str:
    return os.path.join(_TRANSCRIPT_DIR, f"{_safe_name(sender)}.jsonl")


def append(sender: str, role: str, text: str) -> None:
    """Append one event to the sender's transcript file. Never raises."""
    try:
        _ensure_dir()
        record = json.dumps({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "role": role,
            "text": text,
        })
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
