"""Reentrant process/thread lock for cooperating local file writers."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import threading
import time


_registry_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_held = threading.local()


@contextmanager
def file_lock(path: Path, *, timeout: float = 10.0):
    """Lock one persistent sidecar; never unlink it (other processes may hold it)."""
    path = Path(path).resolve()
    key = os.path.normcase(str(path))
    deadline = time.monotonic() + timeout
    with _registry_guard:
        guard = _thread_locks.setdefault(key, threading.RLock())
    if not guard.acquire(timeout=max(0.0, timeout)):
        raise TimeoutError(f"保存処理が使用中です: {path}")
    try:
        held = getattr(_held, "paths", None)
        if held is None:
            held = _held.paths = set()
        if key in held:
            yield
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as stream:
            if os.name == "nt":
                import msvcrt
                if stream.seek(0, 2) == 0:
                    stream.write(b"\0")
                    stream.flush()
            else:
                import fcntl
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"保存処理が使用中です: {path}") from error
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        guard.release()
