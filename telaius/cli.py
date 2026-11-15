"""The single Telaius command-line interface."""
import argparse
import asyncio
import contextlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path

from telaius import at as at_module, qmi as qmi_module
from telaius.callspec import AUDIO_PATHS, CallSpec
from telaius.callstore import find_call, list_calls, read_status, request_cancel
from telaius.config import REPO_ROOT, calls_path, load_config, setting
from telaius.errors import ModemBusy, ModemNotFound, ModemNotReady, PolicyDenied, TelaiusError
from telaius.lock import ModemLock
from telaius.logs import setup
from telaius.policy import from_config
from telaius.prompts import LANGS
from telaius.realtime import probe
from telaius.session import run_call
from telaius.status import modem_status, relay_status
from telaius.sms import delete_sms, list_sms, read_sms, send_sms
from telaius.worker import start_call

EXIT = {ModemBusy: 3, PolicyDenied: 4, ModemNotFound: 5, ModemNotReady: 5}


def parser():
    ap = argparse.ArgumentParser(prog="telaius")
    ap.add_argument("--json", action="store_true", dest="json_output")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    call = sub.add_parser("call")
    call.add_argument("number")
    for name in ("task", "callee", "context", "constraints", "voice"):
        call.add_argument(f"--{name}")
    call.add_argument("--lang", choices=LANGS)
    call.add_argument("--audio", choices=AUDIO_PATHS)
    call.add_argument("--max-seconds", type=float)
    call.add_argument("--formal", action="store_true")
    call.add_argument("--result-schema", type=Path)
    call.add_argument("--detach", action="store_true")
    calls = sub.add_parser("calls").add_subparsers(dest="calls_command", required=True)
    listing = calls.add_parser("list"); listing.add_argument("--limit", type=int, default=20)
    show = calls.add_parser("show"); show.add_argument("id"); show.add_argument("--transcript", action="store_true")
    wait = calls.add_parser("wait"); wait.add_argument("id"); wait.add_argument("--timeout", type=float)
    end = calls.add_parser("end"); end.add_argument("id")
    probe_p = sub.add_parser("probe"); probe_p.add_argument("--lang", choices=LANGS); probe_p.add_argument("--voice")
    at = sub.add_parser("at"); at.add_argument("--wait", type=float, default=5); at.add_argument("cmds", nargs="+")
    qmi = sub.add_parser("qmi"); qmi.add_argument("qmi_command", choices=["status", "mbn-list", "mbn-select"]); qmi.add_argument("id", nargs="?")
    relay = sub.add_parser("relay").add_subparsers(dest="relay_command", required=True)
    relay.add_parser("status"); relay.add_parser("install")
    sms = sub.add_parser("sms").add_subparsers(dest="sms_command", required=True)
    send = sms.add_parser("send"); send.add_argument("number"); send.add_argument("text", nargs="?"); send.add_argument("--file", type=Path)
    listing = sms.add_parser("list"); listing.add_argument("--unread", action="store_true"); listing.add_argument("--all-storages", action="store_true")
    read = sms.add_parser("read"); read.add_argument("id")
    delete = sms.add_parser("delete"); delete.add_argument("id", nargs="?"); delete.add_argument("--read", action="store_true"); delete.add_argument("--all", action="store_true", dest="all_messages")
    return ap


def emit(value, as_json=False):
    if as_json:
        print(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def _config_spec(a, cfg):
    defaults = cfg.get("call", cfg)
    raw_max = a.max_seconds if a.max_seconds is not None else float(setting("max_seconds", defaults.get("max_seconds", 120), cfg))
    schema = json.loads(a.result_schema.read_text()) if a.result_schema else None
    return CallSpec(number=a.number, lang=a.lang or setting("lang", defaults.get("lang", "ru"), cfg),
                    voice=a.voice or setting("voice", defaults.get("voice", "rex"), cfg),
                    audio=a.audio or setting("audio", defaults.get("audio", "auto"), cfg), max_seconds=raw_max,
                    task=a.task, callee=a.callee, context=a.context, constraints=a.constraints,
                    formal=a.formal, result_schema=schema)


def _wait(directory, timeout=None):
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        status = read_status(directory)
        if status["state"] in ("ended", "failed"):
            result = directory / "result.json"
            return json.loads(result.read_text()) if result.exists() else status
        if deadline is not None and time.monotonic() >= deadline:
            return status
        time.sleep(0.2)


def run(a):
    cfg = load_config(); calls_dir = calls_path(cfg)
    if a.command == "status": return modem_status()
    if a.command == "call":
        spec = _config_spec(a, cfg)
        policy = from_config(cfg, calls_dir)
        if a.detach: return {"call_id": start_call(spec, calls_dir, policy).name, "state": "created"}
        with ModemLock(f"call to {spec.callee or 'number'}"):
            return asyncio.run(run_call(spec, calls_dir, policy=policy))
    if a.command == "calls":
        if a.calls_command == "list": return list_calls(calls_dir, a.limit)
        directory = find_call(a.id, calls_dir)
        if a.calls_command == "wait": return _wait(directory, a.timeout)
        if a.calls_command == "end": request_cancel(directory); return {"ok": True, "status": read_status(directory)}
        status = read_status(directory)
        result = directory / "result.json"
        output = json.loads(result.read_text()) if result.exists() else status
        if a.transcript and (directory / "transcript.txt").exists(): output["transcript_text"] = (directory / "transcript.txt").read_text()
        return output
    if a.command == "probe":
        spec = CallSpec(lang=a.lang or cfg.get("lang", "ru"), voice=a.voice or setting("voice", "rex", cfg))
        out = REPO_ROOT / "recordings"; out.mkdir(exist_ok=True); asyncio.run(probe(spec, out)); return {"ok": True}
    if a.command == "at":
        output = io.StringIO()
        with ModemLock("at console"), contextlib.redirect_stdout(output): at_module.run(a)
        return {"output": output.getvalue()} if a.json_output else output.getvalue()
    if a.command == "qmi":
        a.cmd = a.qmi_command
        output = io.StringIO()
        with ModemLock("qmi"), contextlib.redirect_stdout(output): qmi_module._run_cmd(parser(), a)
        return {"output": output.getvalue()} if a.json_output else output.getvalue()
    if a.command == "sms":
        if a.sms_command == "send":
            if a.file and a.text is not None:
                raise ValueError("provide text or --file, not both")
            if a.file:
                text = sys.stdin.read() if str(a.file) == "-" else a.file.read_text()
            elif a.text == "-":
                text = sys.stdin.read()
            elif a.text is not None:
                text = a.text
            else:
                raise ValueError("SMS text, --file, or - is required")
            return send_sms(a.number, text, cfg)
        if a.sms_command == "list": return list_sms(a.unread, a.all_storages, cfg)
        if a.sms_command == "read": return read_sms(a.id, cfg)
        if not (a.id or a.read or a.all_messages): raise ValueError("provide an ID, --read, or --all")
        return delete_sms([a.id] if a.id else None, a.read, a.all_messages, cfg)
    if a.relay_command == "status": return relay_status()
    subprocess.run([str(REPO_ROOT / "relay/install.sh")], check=True); return relay_status()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--json" in argv:
        argv.remove("--json")
        argv.insert(0, "--json")
    ap = parser(); a = ap.parse_args(argv); setup()
    try:
        result = run(a)
        if result is not None: emit(result, a.json_output)
        waits_for_call = a.command == "call" or (a.command == "calls" and a.calls_command == "wait")
        if waits_for_call and isinstance(result, dict):
            if result.get("final_status") in ("no_answer", "busy"):
                return 10
            if result.get("final_status") == "failed":
                return 1
        return 0
    except TelaiusError as error:
        emit({"ok": False, "error": {"code": error.code, "message": str(error)}} if a.json_output else f"{error.code}: {error}", a.json_output)
        return next((code for kind, code in EXIT.items() if isinstance(error, kind)), 1)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        emit({"ok": False, "error": {"code": "error", "message": str(error)}} if a.json_output else f"error: {error}", a.json_output)
        return 1


if __name__ == "__main__":
    sys.exit(main())
