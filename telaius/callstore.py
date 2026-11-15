"""Where a call lives on disk: calls/<date>_<time>_<who>/, created when the call starts.

While the call runs: spec.json (what was asked), status.json (where it is now, rewritten on every
state change) and call.log. When it ends: call.mp3, transcript.txt and result.json.

calls/ is gitignored: it holds real people's voices and phone numbers.
"""
import array
import json
import os
import shutil
import signal
import subprocess
import time
import unicodedata
import wave
from pathlib import Path

from telaius.callspec import CallSpec
from telaius.errors import CallNotFound

CALLS_DIR = Path(__file__).resolve().parents[1] / "calls"
MP3_BITRATE = "48k"  # plenty for 16 kHz mono speech

SPEC_FILE = "spec.json"
STATUS_FILE = "status.json"
CANCEL_FILE = "cancel"
LOG_FILE = "call.log"

_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                      "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


_EXTRA = {"ł": "l", "ß": "ss", "æ": "ae", "ø": "o", "đ": "d"}


def _ascii(word):
    """Cyrillic by table, Latin with diacritics (Polish ą ć ę ł ń ó ś ź ż ...) by stripping marks."""
    out = []
    for c in word:
        c = _TRANSLIT.get(c, _EXTRA.get(c, c))
        out.append(unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode())
    return "".join(out)


def slug(callee, number):
    """'Маша, коллега Флопа' -> 'masha'; without a callee, the last 4 digits of the number."""
    word = (callee or "").replace(",", " ").split()
    if word:
        s = _ascii(word[0].lower())
        s = "".join(c for c in s if c.isascii() and (c.isalnum() or c in "-_"))
        if s:
            return s
    return (number or "call")[-4:]


def call_dir(callee, number, root=CALLS_DIR, started=None):
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(started))
    base = Path(root) / f"{stamp}_{slug(callee, number)}"
    path, n = base, 2
    while path.exists():  # two calls in the same second keep their own directories
        path, n = base.with_name(f"{base.name}-{n}"), n + 1
    return path


def write_spec(directory, spec):
    Path(directory).joinpath(SPEC_FILE).write_text(json.dumps(spec.to_dict(), ensure_ascii=False, indent=2))


def read_spec(directory):
    path = Path(directory) / SPEC_FILE
    if not path.exists():
        raise CallNotFound(f"no {SPEC_FILE} in {directory}")
    return CallSpec.from_dict(json.loads(path.read_text()))


def write_status(directory, status):
    """Rewrite status.json in one step, so a reader never sees half a file."""
    directory = Path(directory)
    tmp = directory / (STATUS_FILE + ".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2))
    os.replace(tmp, directory / STATUS_FILE)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_status(directory):
    """The call's status. A worker that died without writing an end is reported as failed."""
    path = Path(directory) / STATUS_FILE
    if not path.exists():
        raise CallNotFound(f"no {STATUS_FILE} in {directory}")
    status = json.loads(path.read_text())
    pid = status.get("pid")
    if status.get("state") not in ("ended", "failed") and pid and not pid_alive(pid):
        status.update(state="failed", final_status="failed", end_reason="error: worker died")
    return status


def request_cancel(directory, reason="cancelled"):
    """Ask a running call to end: a flag file its session polls, plus SIGTERM to another process.

    Returns the status the call had when asked; an already finished call is left alone.
    """
    directory = Path(directory)
    status = read_status(directory)
    if status.get("state") in ("ended", "failed"):
        return status
    (directory / CANCEL_FILE).write_text(reason)
    pid = status.get("pid")
    if pid and pid != os.getpid() and pid_alive(pid):
        os.kill(pid, signal.SIGTERM)
    return status


def mix(rx, tx):
    """Both sides of the call in one mono track (s16le bytes)."""
    a, b = array.array("h", bytes(rx)), array.array("h", bytes(tx))
    n = max(len(a), len(b))
    a.extend([0] * (n - len(a)))
    b.extend([0] * (n - len(b)))
    return array.array("h", (max(-32768, min(32767, x + y)) for x, y in zip(a, b))).tobytes()


def write_audio(path_stem, rate, pcm):
    """call.mp3 via ffmpeg; falls back to call.wav when ffmpeg is not installed. Returns the file name."""
    if shutil.which("ffmpeg"):
        out = path_stem.with_suffix(".mp3")
        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(rate),
                            "-ac", "1", "-i", "-", "-codec:a", "libmp3lame", "-b:a", MP3_BITRATE, str(out)],
                           input=pcm, capture_output=True)
        if r.returncode == 0:
            return out.name
    out = path_stem.with_suffix(".wav")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.name


def transcript_text(record):
    def mmss(t):
        return f"{int(t) // 60:02d}:{int(t) % 60:02d}"

    who = record.get("callee") or record.get("number")
    lines = [
        f"Звонок: {who} ({record.get('number')})",
        f"Начало: {record.get('started_at')}, длительность {record.get('duration_s')} с, сеть {record.get('network')}, "
        f"звук {record.get('audio')} {record.get('rate')} Гц",
        f"Задача: {record.get('task') or '— (свободный разговор)'}",
        f"Итог: {record.get('outcome')} · завершение: {record.get('end_reason')}",
        f"Резюме: {record.get('summary') or '—'}",
        "",
    ]
    names = {"bot": record.get("bot_name") or "Telaius", "user": (record.get("callee") or "Собеседник").split(",")[0]}
    for t in record.get("transcript", []):
        lines.append(f"[{mmss(t['t'])}] {names.get(t['role'], t['role'])}: {t['text']}")
    return "\n".join(lines) + "\n"


def save_call(directory, rate, rx, tx, record):
    """Write call.mp3, transcript.txt and result.json; returns the directory."""
    directory.mkdir(parents=True, exist_ok=True)
    record["audio_file"] = write_audio(directory / "call", rate, mix(rx, tx)) if (rx or tx) else None
    (directory / "result.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
    (directory / "transcript.txt").write_text(transcript_text(record))
    return directory
