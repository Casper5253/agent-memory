"""Atomic writes, create-once writes and cross-process file locks (Windows and POSIX)."""
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class LockTimeout(Exception):
    pass


def atomic_write(path: Path, text: str) -> None:
    """Write via a sibling temp file and os.replace, so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(20):  # Windows: a reader (Obsidian, indexer) may hold the target briefly
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def create_once(path: Path, text: str) -> bool:
    """Create path atomically only if it does not exist. Returns False if it already existed.

    Session notes are immutable: this is the only way they are written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, path)  # fails if path exists: no window where two writers both "win"
        except FileExistsError:
            return False
        except OSError:
            # Cooperating writers use the same destination lock. Replace publishes only
            # a fully fsynced sibling; receipt/hash validation gates capture adoption.
            with file_lock(path.with_name(path.name + ".create.lock")):
                if path.exists():
                    return False
                os.replace(tmp, path)
        return True
    finally:
        Path(tmp).unlink(missing_ok=True)


@contextmanager
def file_lock(path: Path, timeout: float = 30.0, poll: float = 0.1):
    """Exclusive advisory lock on `path` (created if needed). Released on exit or process death."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                if os.name == "nt":
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(str(path))
                time.sleep(poll)
        yield
    finally:
        try:
            if os.name == "nt":
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def try_lock(path: Path):
    """Non-blocking lock attempt; returns a context manager or None if someone else holds it."""
    try:
        cm = file_lock(path, timeout=0.0)
        cm.__enter__()
        return cm
    except LockTimeout:
        return None
