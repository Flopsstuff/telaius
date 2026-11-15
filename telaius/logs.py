"""Logging for the whole package: stderr, plus call.log while a call runs.

Nothing in the library writes to stdout — it belongs to machine-readable output (CLI --json,
later the MCP stdio protocol). Lines keep the seconds-since-start prefix of the prototypes,
which is what makes call timing readable.
"""
import contextlib
import logging
import sys

log = logging.getLogger("telaius")

FORMAT = "[%(elapsed)7.2f] %(message)s"


class _Elapsed(logging.Formatter):
    def format(self, record):
        record.elapsed = record.relativeCreated / 1000
        return super().format(record)


def setup(verbose=False, stream=sys.stderr):
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not any(getattr(h, "_telaius_stderr", False) for h in log.handlers):
        handler = logging.StreamHandler(stream)
        handler.setFormatter(_Elapsed(FORMAT))
        handler._telaius_stderr = True
        log.addHandler(handler)
    return log


@contextlib.contextmanager
def to_file(path):
    """Also write the log to `path` (the call's own call.log) for as long as the block runs."""
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(_Elapsed(FORMAT))
    log.addHandler(handler)
    try:
        yield
    finally:
        log.removeHandler(handler)
        handler.close()
