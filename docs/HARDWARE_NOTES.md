# Hardware Notes — what is proven vs. still open

Findings from the bring-up sessions (2026-10-24/25). Tested from macOS over raw libusb; the Raspberry Pi target is not tested yet.

## Tested unit

| Item | Value |
|---|---|
| Board | DFRobot TEL0162 "SIM7600G-H CAT4 4G Communication Module V1.0" ([wiki](https://wiki.dfrobot.com/tel0162/), [schematic](https://dfimg.dfrobot.com/wiki/21784/TEL0162_sim7600g-h-4g-communication-module_schematics_V1.0.pdf)) |
| Module | SIMCOM_SIM7600G-H, P/N `S2-1097D-Z307J` R2. Firmware: factory `LE20B03SIM7600M22`; on 2026-10-25 flashed to `LE20B06SIM7600G22`, then to the SIMCom USB PCM hotfix **`LE20B06SIM7600G22_260714`** (current) ([VOLTE_NOTES.md §5](VOLTE_NOTES.md)) |
| USB | `1e0e:9001`, 6 vendor-specific interfaces (`if0`..`if5`), powered from USB Type-C through a hub |
| SIM / network | Orange PL (260-03) and T-Mobile PL (260-02), LTE bands 1/3/20 |

## USB interfaces

| Interface | Use | Notes |
|---|---|---|
| if2 | AT commands | Bulk endpoints; DTR asserted via CDC `SET_CONTROL_LINE_STATE` |
| if4 | Call audio, raw PCM | EP IN `0x88`, EP OUT `0x05`; s16le mono; 320 B (8 kHz) or 640 B (16 kHz) per 20 ms; B03: 2G/CSFB calls only; B06: VoLTE too, uplink unreliable; hotfix: VoLTE both ways, 8 kHz only |
| if5 | QMI | CDC encapsulated commands; needs DTR; used by `telaius.qmi` |
| if6 | ADB | only after `AT+CUSBADB=1` + reset; root shell into the module's Linux |

macOS has no driver for `1e0e:9001`, so no `/dev/cu.*` ports appear; `telaius.modem` claims the interfaces directly with pyusb. On Linux the `option` driver normally exposes them as `/dev/ttyUSB*`, but device numbering is not guaranteed.

## Proven (M0 / M1)

- Outbound voice call: `ATD<number>;` → dialing → alerting → `VOICE CALL: BEGIN`; `AT+CHUP` hangs up.
- Two-way digital call audio over USB, simultaneously, without microphone or speaker:
  - send `AT+CPCMREG=1` **after** `VOICE CALL: BEGIN`, and `AT+CPCMREG=0` after the call;
  - the modem delivers far-end audio on if4 and plays whatever is written to if4, paced at 20 ms frames;
  - 25 s test: 0 transfer errors; far-end confirmed hearing the injected phrase; recording intelligible, no artifacts.
- With `CPCMREG=1` the onboard analog path (NAU88C10 codec + onboard mic) is disconnected from the call.
- Live conversation with xAI Realtime (`grok-voice-latest`, PCM at the modem rate, `server_vad`):
  RU and PL conversations up to 2.5 min, language switching on request, barge-in, model-initiated hangup via an `end_call` function tool.
  Server-side latency from end of user speech to first bot audio: ~0.6–0.9 s (more when the caller pauses mid-sentence).

## Gotchas

- **Default audio routing** is the onboard codec (`AT+CSDVC=1`, `AT+CPCMREG=0`): without switching, the callee hears the board microphone.
- **`AT+CPCMFRM` is sticky**: after setting `1` (16 kHz) the modem answers `ERROR` to `AT+CPCMFRM=0` until reset. Code must read `AT+CPCMFRM?` and use that rate on both sides; a mismatch produces sped-up "chipmunk" audio.
- 16 kHz PCM does not improve quality while calls run over GSM: >98% of the recorded energy is below 3.4 kHz.
- The blue NETLIGHT LED is hardwired (module pin 51 → Q1 → LED1); this firmware has no `AT+CNETLIGHT`, and `AT+CLEDITST` has no visible effect. Only removing R10/R12 disables it.

## VoLTE

See [VOLTE_NOTES.md](VOLTE_NOTES.md) for the full story. In short:

- Without IMS registration, calls fall back to **2G GSM** (CSFB): narrowband, fragile on weak GSM cells, and gone once 2G is switched off.
- T-Mobile PL needs VoLTE enabled per number (SMS `TAK` to `80535`). Before that, the IMS PDN is rejected with ESM #33. Orange PL is still #33 on the tested SIM.
- IMS registration state is only visible over QMI (`python -m telaius.qmi status`). Selecting the `Commercial-DT` MBN (`python -m telaius.qmi mbn-select …`) got T-Mobile PL registered.
- VoLTE calls work, in wideband AMR-WB. On factory B03, **USB PCM (`AT+CPCMREG`) carries no audio on VoLTE** because `alsa_daemon` taps the wrong voice session.
- After flashing **B06**, USB PCM on VoLTE works partly: far end → host is reliable (16 kHz wideband; watch for a stray leading byte), but host → far end often stays silent ([VOLTE_NOTES.md §3.5](VOLTE_NOTES.md)).
- On the **hotfix `_260714`** (current), USB PCM on VoLTE works reliably both ways at **8 kHz**: 3/3 consecutive bridge calls ([VOLTE_NOTES.md §3.6](VOLTE_NOTES.md)).
- Digital VoLTE audio works through the module's Linux over ADB (`AT+CUSBADB=1`): incall record (`VOC_REC_DL` → `hw:0,0`) and incall music (`Incall_Music` → `hw:0,13`).

## Open risks

- 16 kHz (wideband) in both directions runs through `telaius-relay` on the module (incall record/music ↔ if4 / `/dev/ttyGS0`, board mic muted via codec Sleep): `voicebridge --audio relay`. It is installed over ADB with `relay/install.sh`, and the boot autostart lives in the module's `/etc/inittab`, so it is lost on a `system` reflash ([VOLTE_NOTES.md §3.8](VOLTE_NOTES.md)).
- Orange PL VoLTE is unresolved (#33), and CSFB on T-Mobile is unreliable at the test location.
- Not yet tested on the Raspberry Pi (USB power budget: the board can draw up to ~2 A peaks; consider the 5–12 V terminal input).
- No graceful wrap-up before the max call duration yet; the call is cut abruptly.
- Callee name/context must be passed explicitly; the model otherwise may invent one.
