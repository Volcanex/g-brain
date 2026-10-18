"""Store transactions and atomic Markdown replacement."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path


def _match_owner(path, directory):
    """A root writer must leave files the store's owning user can still write."""
    if os.geteuid() == 0:
        stat = directory.stat()
        os.chown(path, stat.st_uid, stat.st_gid)


def _ensure_dir(directory):
    """Create a store directory owned like its parent, so root never claims it."""
    missing = []
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for path in reversed(missing):
        path.mkdir(exist_ok=True)
        _match_owner(path, path.parent)


@contextmanager
def store_lock(directory):
    _ensure_dir(directory)
    with (directory / ".lock").open("a") as lock:
        _match_owner(directory / ".lock", directory)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def atomic_write(path, text):
    _ensure_dir(path.parent)
    try:
        mode = path.stat().st_mode & 0o7777
    except FileNotFoundError:
        mode = None
    temporary = None
    try:
        for attempt in range(100):
            candidate = path.parent / f".{path.name}.{os.getpid()}.{attempt}.tmp"
            try:
                # 0o666 lets the process umask decide a new record's mode,
                # without reading the umask through a process-wide change.
                descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            except FileExistsError:
                continue
            temporary = candidate
            break
        else:
            raise FileExistsError(f"no free temporary name beside {path}")
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            if mode is not None:
                os.fchmod(stream.fileno(), mode)
        _match_owner(temporary, path.parent)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
