"""Store transactions and atomic Markdown replacement."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import tempfile


def _match_owner(path, directory):
    """A root writer must leave files the store's owning user can still write."""
    if os.geteuid() == 0:
        stat = directory.stat()
        os.chown(path, stat.st_uid, stat.st_gid)


@contextmanager
def store_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as lock:
        _match_owner(directory / ".lock", directory)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        try:
            os.chmod(temporary, path.stat().st_mode & 0o7777)
        except FileNotFoundError:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(temporary, 0o666 & ~umask)
        _match_owner(temporary, path.parent)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
