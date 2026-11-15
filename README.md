# Telaius

![Telaius](assets/telaius.jpg)

Self-hosted outbound telephone calling for AI agents: a SIMCom SIM7600G-H modem places a real mobile call, and the xAI Realtime voice API holds the conversation. See [docs/TELAIUS_CONCEPT.md](docs/TELAIUS_CONCEPT.md) for the vision, [docs/HARDWARE_NOTES.md](docs/HARDWARE_NOTES.md) for what is proven on real hardware, and [docs/VOLTE_NOTES.md](docs/VOLTE_NOTES.md) for IMS/VoLTE findings.

**Status:** M0 (USB call + two-way audio) and M1 (live AI conversation) work as CLI prototypes. M2 (MCP interface) is not started.

## Setup

Requires Python 3.11+. Linux uses the kernel's `option` and `qmi_wwan` devices; macOS uses the
raw libusb fallback (`brew install libusb`). `adb` is also needed to install the optional 16 kHz relay.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
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
.venv/bin/telaius at ATI AT+CPIN? AT+CPSI?
.venv/bin/telaius qmi status
.venv/bin/telaius qmi mbn-list
.venv/bin/telaius relay status
.venv/bin/telaius relay install
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

Each call writes `calls/<date>_<time>_<callee>/` (gitignored) with `call.mp3` (both sides, via ffmpeg; `call.wav` if ffmpeg is missing), a readable `transcript.txt` and `result.json`: outcome (`done` / `partial` / `failed` / `no_task` / `unknown`), summary for the owner, end reason (`bot_goodbye`, `remote_hangup`, `max_duration`, `not_answered: …`, `cancelled`), network and the full transcript. The bot reports through its `end_call` tool. If the callee hangs up first, the bot is asked for the result afterwards (`report_result`).

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

Prompts are config files in `prompts/`: `ru.toml` / `pl.toml` hold each language's texts (free chat, intros incl. `intro_formal`, the `[task]` template and rules, recording facts, voice tags, gender lines), `settings.toml` holds the owner's name, the bot name per voice gender (Telaius / Телайя), voice genders, transcription hints and tool descriptions. Adding `prompts/<lang>.toml` adds a language to `--lang`. `TELAIUS_PROMPTS_DIR` points to another folder; `--prompt-file` replaces the prompt for one call.

Calls go to `calls/`, and test audio (`calltest`, `--no-call`) goes to `recordings/`; both are gitignored. Only call numbers whose owners agreed to be called by an AI assistant.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

No modem and no network needed: whole calls run against a fake modem (AT and PCM ports, so the real
AT parsing and audio threads are exercised) and a fake xAI Realtime server — answered, unanswered,
busy, hung up by the callee, cancelled from outside, and a modem that is already in use.
