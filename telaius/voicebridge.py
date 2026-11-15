"""Entry point for one AI call, a thin wrapper over the call core in telaius.session.

Usage:
  python -m telaius.voicebridge --no-call                      # test the xAI session only, save the greeting
  python -m telaius.voicebridge NUMBER [--lang ru|pl] [--voice rex] [--audio usb|relay] [--max-seconds N]
  python -m telaius.voicebridge NUMBER --task "Записаться на стрижку в субботу после 12" \
      --callee "Барбершоп Brzytwa" --context "Имя клиента: Алексей" --audio relay
  python -m telaius.voicebridge NUMBER --task "…" --detach    # call in its own process, print its directory

Without --task the bot holds a free test chat. Each call writes calls/<date>_<time>_<callee>/ (gitignored)
with status.json while it runs and call.mp3 (both sides), transcript.txt and result.json at the end.

--audio usb    SIMCom USB PCM (AT+CPCMREG), rate follows AT+CPCMFRM (8 kHz on the hotfix firmware)
--audio relay  telaius-relay running on the module (relay/), 16 kHz both ways, no AT+CPCMREG

A running call ends early on `touch <call dir>/cancel` or SIGTERM to the worker: the bot is asked
for a result and the record is saved as usual.

XAI_API_KEY is read from the environment or from .env in the repository root (TELAIUS_ENV_FILE).
"""
import argparse
import asyncio
import sys
from pathlib import Path

from telaius.callspec import AUDIO_PATHS, CallSpec
from telaius.callstore import CALLS_DIR
from telaius.config import REPO_ROOT, env_value
from telaius.errors import TelaiusError
from telaius.lock import ModemLock
from telaius.logs import log, setup
from telaius.prompts import LANGS
from telaius.realtime import probe
from telaius.session import run_call
from telaius.worker import start_call


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("number", nargs="?")
    ap.add_argument("--no-call", action="store_true", help="only test the xAI session")
    ap.add_argument("--task", help="what the call should achieve, in plain words (without it: free test chat)")
    ap.add_argument("--callee", help="who is being called (name, role)")
    ap.add_argument("--context", help="facts the bot may use (dates, references, preferences)")
    ap.add_argument("--prompt-file", type=Path, help="full system prompt, replaces the built-in one")
    ap.add_argument("--formal", action="store_true",
                    help="calling an organization: open with the question, then say it's an AI assistant; formal address")
    ap.add_argument("--lang", choices=LANGS, default="ru")
    ap.add_argument("--voice", default=env_value("TELAIUS_VOICE", "rex"),
                    help="xAI voice: rex, leo, sal (male), eve, ara (female); default TELAIUS_VOICE from .env, else rex")
    ap.add_argument("--audio", choices=AUDIO_PATHS, default="usb", help="call audio path (see above)")
    ap.add_argument("--max-seconds", type=float, default=120)
    ap.add_argument("--detach", action="store_true",
                    help="run the call in its own process and print its directory instead of waiting")
    ap.add_argument("--out-dir", type=Path, default=CALLS_DIR,
                    help="each call gets its own subfolder with call.mp3, transcript.txt and result.json")
    a = ap.parse_args()
    if not a.number and not a.no_call:
        ap.error("NUMBER or --no-call required")
    setup()
    try:
        spec = CallSpec(number=a.number, lang=a.lang, voice=a.voice, task=a.task, callee=a.callee,
                        context=a.context, formal=a.formal, audio=a.audio, max_seconds=a.max_seconds,
                        prompt=a.prompt_file.read_text() if a.prompt_file else None)
        if a.no_call:
            log.info("instructions:\n%s", spec.instructions())
            (REPO_ROOT / "recordings").mkdir(exist_ok=True)
            asyncio.run(probe(spec, REPO_ROOT / "recordings"))
            return
        a.out_dir.mkdir(parents=True, exist_ok=True)
        if a.detach:
            print(start_call(spec, a.out_dir))  # stdout: the call directory, for scripts
            return
        with ModemLock(f"call to {a.callee or 'number'}"):
            asyncio.run(run_call(spec, a.out_dir))
    except TelaiusError as e:
        sys.exit(f"{e.code}: {e}")


if __name__ == "__main__":
    main()
