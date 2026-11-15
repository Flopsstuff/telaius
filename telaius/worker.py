"""The process behind a background call: `python -m telaius.worker CALL_DIR`.

A detached call (`telaius call --detach`, later the MCP `make_call`) runs here: the caller writes
the call directory, starts this worker and returns the call id at once, and the call survives the
caller exiting. SIGTERM/SIGINT cancel the call the polite way, through CallSession.cancel().

The worker takes the modem lock itself — an flock cannot be handed to another process — so a call
that loses the race to a foreground call ends as failed in its own status.json.
"""
import asyncio
import signal
import subprocess
import sys
from pathlib import Path

from telaius.callstore import LOG_FILE, call_dir, read_spec, read_status, write_spec, write_status
from telaius.errors import TelaiusError
from telaius.lock import ModemLock
from telaius.logs import log, setup
from telaius.session import CallSession, stamp


def start_call(spec, out_root):
    """Start a call in its own process; returns its directory (the name is the call id)."""
    ModemLock(f"call to {spec.callee or 'number'}").acquire().release()  # fail fast while the modem is busy
    directory = call_dir(spec.callee, spec.number, out_root)
    directory.mkdir(parents=True, exist_ok=True)
    write_spec(directory, spec)
    write_status(directory, {"call_id": directory.name, "state": "created", "pid": None, "elapsed": 0,
                             "started_at": stamp(), "answered_at": None, "ended_at": None,
                             "final_status": None, "end_reason": None, "error": None})
    with open(directory / LOG_FILE, "a") as out:
        proc = subprocess.Popen([sys.executable, "-m", "telaius.worker", str(directory)],
                                stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
    log.info("call %s started in the background (pid %d)", directory.name, proc.pid)
    return directory


async def _run(session):
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, session.cancel, f"cancelled: {signal.Signals(sig).name}")
    return await session.run()


def _mark_failed(directory, error):
    """Record a failure the session could not record itself (e.g. the modem was busy)."""
    status = read_status(directory)
    if status.get("state") in ("ended", "failed"):
        return
    status.update(state="failed", final_status="failed", end_reason=f"error: {error}",
                  error=f"{type(error).__name__}: {error}", ended_at=stamp())
    write_status(directory, status)


def run_dir(directory):
    """Run the call described by `directory`; returns a process exit code."""
    directory = Path(directory)
    spec = read_spec(directory)
    session = CallSession(spec, directory)
    try:
        with ModemLock(f"call {directory.name}"):
            asyncio.run(_run(session))
    except TelaiusError as e:
        log.error("call %s failed: %s", directory.name, e)
        _mark_failed(directory, e)
        return 1
    except Exception as e:
        log.exception("call %s failed", directory.name)
        _mark_failed(directory, e)
        return 1
    return 0 if session.final_status in ("completed", "cancelled") else 10


def main(argv=None):
    setup()
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.exit("usage: python -m telaius.worker CALL_DIR")
    sys.exit(run_dir(argv[0]))


if __name__ == "__main__":
    main()
