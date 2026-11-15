# Telaius

![Telaius](assets/telaius.jpg)

Self-hosted outbound telephone calling for AI agents: a SIMCom SIM7600G-H modem places a real mobile call, and the xAI Realtime voice API holds the conversation. See [docs/TELAIUS_CONCEPT.md](docs/TELAIUS_CONCEPT.md) for the vision, [docs/HARDWARE_NOTES.md](docs/HARDWARE_NOTES.md) for what is proven on real hardware, and [docs/VOLTE_NOTES.md](docs/VOLTE_NOTES.md) for IMS/VoLTE findings.

**Status:** M0 (USB call + two-way audio) and M1 (live AI conversation) work through the `telaius` CLI, with SMS and Russian/Polish/English calls. M2 (agent integration): the `telaius-mcp` MCP server works end to end: an agent on the Pi delegated a live call with `result_schema` and got the filled `data`, and sent an SMS through it.

## Setup

Requires Python 3.11+. Linux uses the kernel's `option` and `qmi_wwan` devices; macOS uses the
raw libusb fallback (`brew install libusb`). `adb` is also needed to install the optional 16 kHz relay.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .        # or -e '.[mcp]' for the MCP server
cp .env.example .env   # gitignored; fill in XAI_API_KEY, optionally TELAIUS_VOICE
```

### Linux device setup

On Linux, Telaius automatically uses the stable SimTech links for USB interfaces 2 and 4:

```text
/dev/serial/by-id/*SimTech*-if02-port0  # AT commands
/dev/serial/by-id/*SimTech*-if04-port0  # call PCM
/dev/cdc-wdm0                          # QMI
```

The user must belong to `dialout`. Install the included udev rule to grant `dialout` access to
`cdc-wdm` and keep ModemManager away from this modem, then unplug/replug it:

```bash
sudo usermod -aG dialout "$USER"
sudo install -m 0644 udev/99-telaius.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
# log out/in if dialout was just added, then reconnect the modem
```

If ModemManager is already managing the modem, stop it before the first reconnect
(`sudo systemctl disable --now ModemManager`) or rely on the installed ignore rule. Set
`TELAIUS_TRANSPORT=tty` to require kernel devices or `TELAIUS_TRANSPORT=usb` to force the raw
libusb fallback. Automatic mode prefers kernel devices when they are present.

## CLI

The package installs one `telaius` command. Global `--json` may appear before or after the
subcommand; it writes exactly one JSON document to stdout while logs stay on stderr.

```bash
.venv/bin/telaius status --json
.venv/bin/telaius call +48XXXXXXXXX --task "Ask about opening hours" --callee "shop"
.venv/bin/telaius call +48XXXXXXXXX --detach --audio auto
.venv/bin/telaius calls list
.venv/bin/telaius calls show CALL_ID --transcript
.venv/bin/telaius calls wait CALL_ID --timeout 180
.venv/bin/telaius calls end CALL_ID
.venv/bin/telaius probe --lang ru --voice rex
.venv/bin/telaius call +48XXXXXXXXX --lang en --voice eve --max-seconds 120
.venv/bin/telaius at ATI AT+CPIN? AT+CPSI?
.venv/bin/telaius qmi status
.venv/bin/telaius qmi mbn-list
.venv/bin/telaius relay status
.venv/bin/telaius relay install
.venv/bin/telaius sms send +48XXXXXXXXX "Hello from Telaius"
.venv/bin/telaius sms send +48XXXXXXXXX --file message.txt
.venv/bin/telaius sms list --all-storages
.venv/bin/telaius sms read SM:12
.venv/bin/telaius sms delete SM:12
```

`call` accepts `--task`, `--callee`, `--context`, `--constraints`, `--lang`, `--voice`,
`--formal`, `--audio auto|usb|relay`, `--max-seconds`, and `--result-schema FILE.json`.
`auto` tries the 16 kHz relay and switches the live xAI session and modem audio to USB if no
relay frames arrive. When the relay is unavailable, this detection adds up to three seconds of
silence before the USB path starts.

Exit codes are `0` success, `1` operational/configuration error, `2` usage, `3` modem busy, `4`
policy denied, and `5` modem missing or not ready. `call` and `calls wait` return `10` for no
answer or busy.

### Configuration and policy

Copy `telaius.example.toml` to the gitignored `telaius.toml`, or select a path with
`TELAIUS_CONFIG`. Secrets stay in `.env`; `TELAIUS_ENV_FILE` selects that file. Command flags
override `TELAIUS_*` environment values, which override TOML and built-in defaults.

Outbound calls are denied unless the normalized destination is in `policy.allowlist`, or
`policy.allow_any = true` is explicit. Policy also enforces the daily call limit, timezone-aware
quiet hours, and a hard duration ceiling. Numbers may be E.164, start with `00`, or contain nine
Polish digits; spaces and hyphens are ignored.

Environment overrides for policy: `TELAIUS_ALLOWLIST` (comma-separated numbers),
`TELAIUS_ALLOW_ANY`, `TELAIUS_POLICY_MAX_SECONDS`, `TELAIUS_POLICY_CALLS_PER_DAY`,
`TELAIUS_POLICY_SMS_PER_DAY`, `TELAIUS_POLICY_TIMEZONE`, `TELAIUS_POLICY_QUIET_START` and
`TELAIUS_POLICY_QUIET_END`. `TELAIUS_MAX_SECONDS` only sets the default call length; the policy
ceiling still clips it. A relative `calls_dir` is resolved from the repository root.

### SMS

SMS uses PDU mode and the same modem lock and outbound allowlist as calls. GSM-7 is used
when possible, including the extension characters `{ } [ ] ~ \\ ^ | €`; all other text
uses UCS2. Long messages are split with 160/153 GSM-7 or 70/67 UCS2 limits and reassembled
from concatenation headers when listed.

`sms send` also accepts `--file FILE`; use `--file -` or text `-` for stdin. Incoming IDs
include their modem storage (`SM:12` or `ME:4`). `sms list` does not alter messages,
`sms read ID` marks one modem entry read, and deletion is always explicit through
`sms delete ID|--read|--all`. Attempts are journaled in gitignored `messages/sms.jsonl`,
which supplies the daily SMS policy counter. The limit counts submitted messages, not
their encoded parts; one concatenated message consumes one daily allowance.

Each call writes `calls/<date>_<time>_<callee>/` (gitignored) with `call.mp3` (both sides, via ffmpeg; `call.wav` if ffmpeg is missing), a readable `transcript.txt` and `result.json`: outcome (`done` / `partial` / `failed` / `no_task` / `unknown`), summary for the owner, `data` and `unresolved` (see `result_schema` under MCP), end reason (`bot_goodbye`, `remote_hangup`, `max_duration`, `not_answered: …`, `cancelled`), network and the full transcript. The bot reports through its `end_call` tool. If the callee hangs up first, the bot is asked for the result afterwards (`report_result`).

### Following and ending a call while it runs

The call directory exists from the moment the call starts and holds `spec.json` (what was asked) and `status.json`, rewritten on every state change and every few seconds in between, so `elapsed` tells a running call from a stuck one:

```json
{ "call_id": "2026-11-15_12-30-00_masha", "state": "in_call", "final_status": null,
  "end_reason": null, "pid": 31337, "started_at": "…", "answered_at": "…", "elapsed": 42.5 }
```

`state` runs `created → dialing → ringing → in_call → reporting → ended`, or `failed` if the call never happened; `final_status` is `completed`, `no_answer`, `busy`, `cancelled` or `failed`. A detached call also writes `call.log`. If the worker process dies, readers see `failed` with `end_reason: "error: worker died"`.

To end a call early, write the `cancel` flag file into its directory (any process can) or send `SIGTERM` to the worker. Either way the modem hangs up, the bot is asked for a result and the record is saved as usual:

```bash
echo "cancelled: owner" > calls/2026-11-15_12-30-00_masha/cancel
```

One modem means one operation at a time: a call, `telaius.at`, `telaius.calltest` and `telaius.qmi` take an exclusive lock (`$XDG_RUNTIME_DIR/telaius/modem.lock`, override with `TELAIUS_RUNTIME_DIR`), so a second one fails with `modem_busy: the modem is held by …` instead of disturbing the live call. The lock disappears with its holder, however it dies.

Logs go to stderr; stdout stays free for machine-readable output. `XAI_API_KEY` is read from the environment or from `.env` in the repository root — `TELAIUS_ENV_FILE` points at another file, which is what a separate worktree needs.

Prompts are config files in `prompts/`: `ru.toml` / `pl.toml` / `en.toml` hold each language's texts (free chat, intros incl. `intro_formal`, the `[task]` template with `constraints`, rules, recording facts, voice tags, gender lines and `[forms]`), `settings.toml` holds the owner's name, the bot name per voice gender (Telaius / Телайя / Telaya), voice genders, transcription hints and tool descriptions. The owner's call summary tool text stays in Russian for every language. Adding `prompts/<lang>.toml` adds a language to `--lang`. `TELAIUS_PROMPTS_DIR` points to another folder; `--prompt-file` replaces the prompt for one call.

Calls go to `calls/`, and test audio (`calltest`, `--no-call`) goes to `recordings/`; both are gitignored. Only call numbers whose owners agreed to be called by an AI assistant.

## MCP

`telaius-mcp` is an MCP server over stdio for AI agents, built on the same core, policy and modem
lock as the CLI. It needs the `mcp` extra (`pip install -e '.[mcp]'`, official MCP Python SDK 2.x).
Add it to Claude Code:

```bash
claude mcp add telaius -- /path/to/telaius/.venv/bin/telaius-mcp
```

Other clients take the usual JSON config; `env` is optional and only needed when the settings
are not in the repository's `.env` / `telaius.toml`:

```json
{
  "mcpServers": {
    "telaius": {
      "command": "/path/to/telaius/.venv/bin/telaius-mcp",
      "env": { "TELAIUS_ENV_FILE": "/path/to/telaius/.env", "TELAIUS_CONFIG": "/path/to/telaius.toml" }
    }
  }
}
```

| tool | input | output |
|---|---|---|
| `get_modem_status` | — | the same as `telaius status --json` |
| `make_call` | `phone_number`, `task`, `language` (`ru`/`pl`/`en`), optional `callee`, `context`, `constraints`, `formal`, `voice`, `max_seconds`, `result_schema` | `{call_id, status}` at once |
| `get_call_status` | `call_id`, `wait_seconds` 0–60 (waits for the next state change) | `state`, `final_status`, `end_reason`, times, `error` |
| `get_call_result` | `call_id`, `include_transcript` (default false) | `ready`, `outcome`, `summary`, `data`, `unresolved`, `end_reason`, `duration_s`, `network`, `recording` and `transcript_file` paths |
| `end_call` | `call_id` | `{ok, state, was_running}`; a finished call is not an error |
| `list_calls` | `limit` | `{calls: [...]}`, newest first, in brief |
| `send_sms` | `phone_number`, `text` | `{ok, parts, message_refs}` |
| `list_sms` | `unread_only`, `limit` | `{messages: [...]}`, long messages joined |

Calls are asynchronous: `make_call` starts the background worker (as `telaius call --detach`
does) and returns; closing the client does not end the call. A second `make_call` while a call
runs is `modem_busy`. Failures are MCP tool errors whose text starts with a code —
`modem_busy`, `modem_not_ready`, `policy_denied`, `call_not_found` or `invalid_input` — followed
by a readable reason. Logs go to stderr; stdout carries only the protocol.

`result_schema` is a JSON Schema with `"type": "object"` (at most 4 KB and 8 levels deep; anything
else is `invalid_input`). It becomes the `data` parameter of the bot's `end_call` and
`report_result` tools, with every top-level field also allowing `null`; both tools always carry
`unresolved`, a list of what the call did not find out. The bot is told to fill `data` only with
what it heard and to answer `null` plus an `unresolved` item rather than guess. Example:

```json
{ "type": "object",
  "properties": { "time": { "type": "string", "description": "when he can talk tomorrow" },
                  "topic": { "type": "string" } },
  "required": ["time", "topic"] }
```

The same schema works from the CLI: `telaius call NUMBER --task "…" --result-schema schema.json`;
`result.json` then has `data` and `unresolved` as well.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

No modem and no network needed: whole calls run against a fake modem (AT and PCM ports, so the real
AT parsing and audio threads are exercised) and a fake xAI Realtime server — answered, unanswered,
busy, hung up by the callee, cancelled from outside, and a modem that is already in use.
The MCP server is tested through an in-process MCP client on the same fakes (with the `mcp`
extra installed; without it those tests are skipped).
