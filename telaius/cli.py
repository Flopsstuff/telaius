"""The single Telaius command-line interface."""
import argparse
import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from telaius import at as at_module, qmi as qmi_module
from telaius.callspec import AUDIO_PATHS, CallSpec
from telaius.callstore import CALLS_DIR, find_call, list_calls, read_status, request_cancel
from telaius.config import REPO_ROOT, load_config, setting
from telaius.errors import ModemBusy, ModemNotFound, ModemNotReady, PolicyDenied, TelaiusError
from telaius.lock import ModemLock
from telaius.logs import setup
from telaius.policy import Policy
from telaius.prompts import LANGS
from telaius.realtime import probe
from telaius.session import run_call
from telaius.status import modem_status, relay_status
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
    return ap


def emit(value, as_json=False):
    if as_json:
        print(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def _config_spec(a, cfg, calls_dir):
    defaults = cfg.get("call", cfg)
    raw_max = a.max_seconds if a.max_seconds is not None else float(setting("max_seconds", defaults.get("max_seconds", 120), cfg))
    policy_config = dict(cfg.get("policy", {}))
    if os.environ.get("TELAIUS_ALLOWLIST"):
        policy_config["allowlist"] = [item.strip() for item in os.environ["TELAIUS_ALLOWLIST"].split(",")]
    if os.environ.get("TELAIUS_ALLOW_ANY"):
        policy_config["allow_any"] = os.environ["TELAIUS_ALLOW_ANY"].lower() in ("1", "true", "yes")
    for key in ("max_seconds", "calls_per_day", "sms_per_day", "timezone"):
        env_name = f"TELAIUS_{key.upper()}"
        if os.environ.get(env_name):
            policy_config[key] = os.environ[env_name]
    policy = Policy(policy_config, calls_dir)
    number, max_seconds, clipped = policy.check_call(a.number, raw_max)
    if clipped:
        print(f"warning: max_seconds limited to {max_seconds:g}", file=sys.stderr)
    schema = json.loads(a.result_schema.read_text()) if a.result_schema else None
    return CallSpec(number=number, lang=a.lang or setting("lang", defaults.get("lang", "ru"), cfg),
                    voice=a.voice or setting("voice", defaults.get("voice", "rex"), cfg),
                    audio=a.audio or setting("audio", defaults.get("audio", "auto"), cfg), max_seconds=max_seconds,
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
    cfg = load_config(); calls_dir = Path(setting("calls_dir", CALLS_DIR, cfg))
    if a.command == "status": return modem_status()
    if a.command == "call":
        spec = _config_spec(a, cfg, calls_dir)
        if a.detach: return {"call_id": start_call(spec, calls_dir).name, "state": "created"}
        with ModemLock(f"call to {spec.callee or 'number'}"):
            return asyncio.run(run_call(spec, calls_dir))
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
        if a.command in ("call", "calls") and isinstance(result, dict) and result.get("final_status") in ("no_answer", "busy"):
            return 10
        return 0
    except TelaiusError as error:
        emit({"ok": False, "error": {"code": error.code, "message": str(error)}} if a.json_output else f"{error.code}: {error}", a.json_output)
        return next((code for kind, code in EXIT.items() if isinstance(error, kind)), 1)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        emit({"ok": False, "error": {"code": "error", "message": str(error)}} if a.json_output else f"error: {error}", a.json_output)
        return 1


if __name__ == "__main__":
    sys.exit(main())
