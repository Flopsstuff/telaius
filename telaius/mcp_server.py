"""The MCP server: `telaius-mcp`, phone calls and SMS as tools for AI agents, over stdio.

A thin layer over the same core as the CLI: calls run in the background worker (make_call returns
at once and the call outlives the client), and the outbound policy and the modem lock are the
ones the CLI uses. stdout carries only the MCP protocol; logs go to stderr.

Tool failures are MCP tool errors whose text starts with a stable code: modem_busy,
modem_not_ready, policy_denied, call_not_found or invalid_input.
"""
import asyncio
import contextlib
import json
import time
from typing import Annotated, Literal

from pydantic import Field

from telaius.callspec import CallSpec
from telaius.callstore import find_call, list_calls as stored_calls, read_spec, read_status, request_cancel
from telaius.config import calls_path, load_config, setting
from telaius.errors import CallNotFound, ConfigError, ModemBusy, ModemNotFound, TelaiusError
from telaius.logs import setup
from telaius.policy import from_config
from telaius.prompts import LANGS
from telaius.sms import list_sms as read_inbox, send_sms as submit_sms
from telaius.status import modem_status
from telaius.worker import start_call

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError as error:  # the SDK is the optional extra telaius[mcp]
    raise SystemExit(f"telaius-mcp needs the MCP SDK: pip install 'telaius[mcp]' ({error})") from error

TERMINAL = ("ended", "failed")
POLL = 0.25  # seconds between status.json reads while get_call_status waits
CODES = {ConfigError: "invalid_input", ModemNotFound: "modem_not_ready"}

INSTRUCTIONS = """\
Telaius places real phone calls and sends real SMS through a mobile modem.

- A call is asynchronous: make_call returns a call_id at once while the call runs in the
  background. Follow it with get_call_status (wait_seconds long-polls for the next change), then
  read the outcome with get_call_result once the state is `ended` or `failed`.
- Call only people who agreed to be called by an AI assistant on the owner's behalf. The bot
  always introduces itself as an AI assistant; do not ask it to pretend otherwise.
- Pass result_schema to get the answers as structured `data`; whatever the bot could not find out
  is listed in `unresolved` instead of being guessed.
- Destinations are limited by the owner's allowlist and quiet hours; one modem serves one call
  or SMS at a time (modem_busy).
"""

server = MCPServer("telaius", instructions=INSTRUCTIONS)

Language = Literal[tuple(LANGS)]


@contextlib.contextmanager
def _tool_errors():
    try:
        yield
    except ToolError:
        raise
    except TelaiusError as error:
        raise ToolError(f"{CODES.get(type(error), error.code)}: {error}") from error
    except ValueError as error:  # includes json.JSONDecodeError
        raise ToolError(f"invalid_input: {error}") from error
    except OSError as error:  # includes usb.core.USBError
        raise ToolError(f"modem_not_ready: {error}") from error


def _calls_dir():
    return calls_path(load_config())


def _active_call(root):
    return next((c["call_id"] for c in stored_calls(root, limit=5) if c.get("state") not in TERMINAL), None)


def _make_call(phone_number, task, callee, context, constraints, language, formal, voice, max_seconds,
               result_schema):
    cfg = load_config()
    root = calls_path(cfg)
    defaults = cfg.get("call", cfg)
    spec = CallSpec(number=phone_number, task=task, callee=callee, context=context, constraints=constraints,
                    lang=language, formal=formal,
                    voice=voice or setting("voice", defaults.get("voice", "rex"), cfg),
                    audio=setting("audio", defaults.get("audio", "auto"), cfg),
                    max_seconds=float(max_seconds if max_seconds is not None
                                      else setting("max_seconds", defaults.get("max_seconds", 120), cfg)),
                    result_schema=result_schema)
    active = _active_call(root)
    if active:
        raise ModemBusy(f"call {active} is still running; wait for it or end it first")
    directory = start_call(spec, root, from_config(cfg, root))
    return {"call_id": directory.name, "status": read_status(directory)["state"]}


@server.tool()
async def get_modem_status() -> dict:
    """Health of the modem: SIM, operator, network (LTE/GSM), signal, IMS (VoLTE), relay, and whether
    it is busy with a call. While a call runs the modem is not queried and `busy` is true."""
    with _tool_errors():
        return await asyncio.to_thread(modem_status)


@server.tool()
async def make_call(
    phone_number: Annotated[str, Field(description="E.164 number, e.g. +48123456789; must be on the owner's allowlist")],
    task: Annotated[str, Field(description="What the call must achieve, in plain words")],
    language: Annotated[Language, Field(description="Language of the call")],
    callee: Annotated[str | None, Field(description="Who is called, e.g. 'Anna, Flop's colleague'")] = None,
    context: Annotated[str | None, Field(description="Facts the bot may use in the call")] = None,
    constraints: Annotated[str | None, Field(description="Hard limits: what the bot must not promise or disclose")] = None,
    formal: Annotated[bool, Field(description="True for companies and institutions: question first, formal address")] = False,
    voice: Annotated[str | None, Field(description="xAI voice (rex, leo, sal, eve, ara); default from config")] = None,
    max_seconds: Annotated[float | None, Field(gt=0, description="Talk time limit; the owner's policy caps it")] = None,
    result_schema: Annotated[dict | None, Field(description=(
        "JSON Schema with type 'object' (up to 4 KB) describing what to find out; the bot fills it in "
        "and get_call_result returns it as `data`, with unknown fields null and listed in `unresolved`"))] = None,
) -> dict:
    """Start a phone call in the background and return {call_id, status} at once.

    The call is asynchronous: follow it with get_call_status and read the outcome with
    get_call_result. Call only people who agreed to it; the bot introduces itself as the owner's AI
    assistant. Closing this client does not end the call; use end_call for that."""
    with _tool_errors():
        return await asyncio.to_thread(_make_call, phone_number, task, callee, context, constraints, language,
                                       formal, voice, max_seconds, result_schema)


@server.tool()
async def get_call_status(
    call_id: str,
    wait_seconds: Annotated[float, Field(ge=0, le=60, description=(
        "Wait up to this long for the state to change (long-poll); 0 returns at once"))] = 0,
) -> dict:
    """Where a call is now: state (created, dialing, ringing, in_call, reporting, ended, failed),
    final_status once it is over (completed, no_answer, busy, cancelled, failed), end_reason,
    times and error."""
    with _tool_errors():
        directory = find_call(call_id, _calls_dir())
        status = read_status(directory)
        seen = (status.get("state"), status.get("final_status"))
        deadline = time.monotonic() + wait_seconds
        while status.get("state") not in TERMINAL and time.monotonic() < deadline:
            await asyncio.sleep(POLL)
            status = read_status(directory)
            if (status.get("state"), status.get("final_status")) != seen:
                break
        status.pop("pid", None)
        return status


def _call_result(call_id, include_transcript):
    directory = find_call(call_id, _calls_dir())
    status = read_status(directory)
    result_file = directory / "result.json"
    if status.get("state") not in TERMINAL or not result_file.exists():
        return {"call_id": call_id, "ready": status.get("state") in TERMINAL, "state": status.get("state"),
                "final_status": status.get("final_status"), "outcome": None, "summary": None, "data": None,
                "unresolved": [], "end_reason": status.get("end_reason"), "error": status.get("error")}
    record = json.loads(result_file.read_text())
    audio, transcript = record.get("audio_file"), directory / "transcript.txt"
    output = {"call_id": call_id, "ready": True, "state": status.get("state"),
              "final_status": record.get("final_status"), "outcome": record.get("outcome"),
              "summary": record.get("summary"), "data": record.get("data"),
              "unresolved": record.get("unresolved", []), "end_reason": record.get("end_reason"),
              "duration_s": record.get("duration_s"), "network": record.get("network"),
              "recording": str(directory / audio) if audio else None,
              "transcript_file": str(transcript) if transcript.exists() else None}
    if include_transcript:
        output["transcript"] = record.get("transcript", [])
    return output


@server.tool()
async def get_call_result(
    call_id: str,
    include_transcript: Annotated[bool, Field(description="Also return the turns of the conversation")] = False,
) -> dict:
    """The outcome of a finished call: outcome (done, partial, failed, no_task), summary for the
    owner, `data` filled by the result_schema, `unresolved`, end_reason, duration, network, and the
    paths of the recording and transcript. While the call still runs, `ready` is false."""
    with _tool_errors():
        return await asyncio.to_thread(_call_result, call_id, include_transcript)


@server.tool()
async def end_call(call_id: str) -> dict:
    """End a running call politely: the bot hangs up and still reports its result. Ending a call that
    is already over is not an error."""
    with _tool_errors():
        directory = find_call(call_id, _calls_dir())
        before = request_cancel(directory, "cancelled: end_call")
        return {"ok": True, "state": read_status(directory).get("state"), "was_running": before.get("state") not in TERMINAL}


def _brief(directory, status):
    try:
        spec = read_spec(directory)
    except (CallNotFound, ConfigError, ValueError):
        spec = None
    return {"call_id": status.get("call_id", directory.name), "state": status.get("state"),
            "final_status": status.get("final_status"), "end_reason": status.get("end_reason"),
            "started_at": status.get("started_at"), "ended_at": status.get("ended_at"),
            "callee": spec.callee if spec else None, "task": spec.task if spec else None}


@server.tool()
async def list_calls(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict:
    """The most recent calls, newest first, in brief."""
    with _tool_errors():
        root = _calls_dir()
        return {"calls": [_brief(root / c.get("call_id", ""), c) for c in stored_calls(root, limit)]}


@server.tool()
async def send_sms(
    phone_number: Annotated[str, Field(description="E.164 number; must be on the owner's allowlist")],
    text: Annotated[str, Field(min_length=1, description="Message text; Cyrillic, Polish and long texts are fine")],
) -> dict:
    """Send an SMS and return {ok, parts, message_refs}. Long or non-Latin texts go as several parts."""
    with _tool_errors():
        result = await asyncio.to_thread(submit_sms, phone_number, text)
        return {key: value for key, value in result.items() if value is not None}


@server.tool()
async def list_sms(
    unread_only: Annotated[bool, Field(description="Only messages not read before")] = False,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict:
    """Received SMS on the SIM, newest first, with long messages joined from their parts."""
    with _tool_errors():
        return {"messages": (await asyncio.to_thread(read_inbox, unread_only))[:limit]}


def main():
    setup()
    server.run("stdio")


if __name__ == "__main__":
    main()
