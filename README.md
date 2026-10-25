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

## Tools

```bash
# AT console (if2 of the modem)
.venv/bin/python -m telaius.at ATI AT+CPIN? AT+CREG? AT+CPSI?

# QMI (if5): IMS registration state and carrier MBN profiles
.venv/bin/python -m telaius.qmi status
.venv/bin/python -m telaius.qmi mbn-list

# M0: call, play a WAV into the call, record the far end
.venv/bin/python -m telaius.calltest +48XXXXXXXXX phrase.wav recordings/rx.wav

# M1: let the AI talk on a real call (ru or pl prompt)
.venv/bin/python -m telaius.voicebridge --no-call          # check the xAI session only
.venv/bin/python -m telaius.voicebridge +48XXXXXXXXX --lang pl --voice rex --max-seconds 120

# 16 kHz VoLTE audio via the relay on the module (one-time: AT+CUSBADB=1, reset, then install).
# relay/telaius-relay is prebuilt (Git LFS); rebuild with: docker build -t telaius-armbuild relay/ && (cd relay && docker run --rm -v "$PWD":/src telaius-armbuild make)
relay/install.sh
.venv/bin/python -m telaius.voicebridge +48XXXXXXXXX --audio relay

# a call with a goal: the bot introduces itself, pursues the task, then reports
.venv/bin/python -m telaius.voicebridge +48XXXXXXXXX --audio relay \
    --task "Записаться на стрижку в субботу после 12:00" \
    --callee "барбершоп Brzytwa" --context "Клиент: Алексей, обычная мужская стрижка"
```

Each call writes `calls/<date>_<time>_<callee>/` (gitignored) with `call.mp3` (both sides, via ffmpeg; `call.wav` if ffmpeg is missing), a readable `transcript.txt` and `result.json`: outcome (`done` / `partial` / `failed` / `no_task` / `unknown`), summary for the owner, end reason (`bot_goodbye`, `remote_hangup`, `max_duration`, `not_answered: …`), network and the full transcript. The bot reports through its `end_call` tool. If the callee hangs up first, the bot is asked for the result afterwards (`report_result`).

Prompts are config files in `prompts/`: `ru.toml` / `pl.toml` hold each language's texts (free chat, intros incl. `intro_formal`, the `[task]` template and rules, recording facts, voice tags, gender lines), `settings.toml` holds the owner's name, the bot name per voice gender (Telaius / Телайя), voice genders, transcription hints and tool descriptions. Adding `prompts/<lang>.toml` adds a language to `--lang`. `TELAIUS_PROMPTS_DIR` points to another folder; `--prompt-file` replaces the prompt for one call.

Calls go to `calls/`, and test audio (`calltest`, `--no-call`) goes to `recordings/`; both are gitignored. Only call numbers whose owners agreed to be called by an AI assistant.
