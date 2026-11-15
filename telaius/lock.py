"""One modem, one user at a time: an exclusive lock around if2/if4/if5.

Two processes talking to the same modem break each other's call, so every modem operation
(call, AT console, QMI) holds this lock. It is an `fcntl.flock` on a runtime file, so the
kernel drops it when the holder dies, however it dies. The file holds the holder's pid and
what it is doing, which is what the loser of the race gets told.
"""
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path

from telaius.errors import ModemBusy


def lock_path():
    if os.environ.get("TELAIUS_RUNTIME_DIR"):
        base = Path(os.environ["TELAIUS_RUNTIME_DIR"])
    elif os.environ.get("XDG_RUNTIME_DIR"):
        base = Path(os.environ["XDG_RUNTIME_DIR"]) / "telaius"
    else:
        base = Path(tempfile.gettempdir()) / f"telaius-{os.getuid()}"
    return base / "modem.lock"


def _holder(fd):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        info = json.loads(os.read(fd, 4096) or "{}")
    except (OSError, json.JSONDecodeError):
        info = {}
    if not info:
        return "held by another process"
    held = f"held by {info.get('purpose') or 'unknown'} (pid {info.get('pid')}"
    if info.get("since"):
        held += f", since {info['since']}"
    return held + ")"


class ModemLock:
    """Context manager; raises ModemBusy instead of waiting."""

    def __init__(self, purpose, path=None):
        self.purpose = purpose
        self.path = Path(path) if path else lock_path()
        self.fd = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            held = _holder(fd)
            os.close(fd)
            raise ModemBusy(f"the modem is {held}") from None
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "purpose": self.purpose,
                                 "since": time.strftime("%Y-%m-%dT%H:%M:%S%z")}).encode())
        self.fd = fd
        return self

    def release(self):
        if self.fd is not None:
            os.ftruncate(self.fd, 0)
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
