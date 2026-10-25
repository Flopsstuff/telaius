# VoLTE Notes — IMS registration and VoLTE call audio on the SIM7600G-H

Findings from 2026-10-24/25 on the unit in [HARDWARE_NOTES.md](HARDWARE_NOTES.md), tested in Poland with Orange PL (260-03) and T-Mobile PL (260-02) SIMs, from macOS over raw libusb and ADB. The module shipped with `LE20B03SIM7600M22` (`SIM7600M22_B03V03_210525`). On 2026-10-25 it was flashed to `LE20B06SIM7600G22` (2025-06-24) and then to SIMCom's USB PCM hotfix **`LE20B06SIM7600G22_260714`** (section 5).

**Summary:**
- VoLTE calls work (IMS registered on T-Mobile PL), in wideband AMR-WB.
- On B03, the documented USB PCM interface (`AT+CPCMREG`) carries no VoLTE audio at all.
- On B06, USB PCM on VoLTE works partly: far end → host is reliable, including 16 kHz wideband, but host → far end works only sometimes (section 3.5).
- **On the hotfix `LE20B06SIM7600G22_260714`, USB PCM on VoLTE works both ways, but only at 8 kHz** (section 3.6). This is the current firmware and the reliable baseline.
- Qualcomm's incall record / incall music routes inside the module carry VoLTE audio both ways at 16 kHz without SIMCom's `alsa_daemon`. **`telaius-relay`** (in `relay/`) runs on the module and bridges them to USB if4: `voicebridge --audio relay` gives 16 kHz both ways on real calls (section 3.8).

## 1. Without VoLTE, calls fall back to 2G

With no IMS registration, every voice call leaves LTE through CSFB:

- `+CPSI: GSM,...,EGSM 900`, `+CNSMOD: 0,1`.
- Audio is narrowband: more than 98% of the energy sits below 3.4 kHz, even at `AT+CPCMFRM=1` (16 kHz).
- CSFB is fragile on a weak GSM cell. On T-Mobile, at around −90…−100 dBm, the dial stayed in `dialing`, the CPSI cell fields went to `0xFFFF/BAND0` while the modem searched for GSM, and after about 30 s it ended with `NO CARRIER` / `+CEER: No service available`. The phone never rang.

## 2. Getting IMS registered

The modem has to complete three things for VoLTE: the operator subscription, the IMS PDN, and the IMS SIP registration.

### 2.1 Subscription (operator side)

- `AT+CGACT=1,2` on the `ims` APN was rejected with `+CME ERROR: requested service option not subscribed` (ESM cause #33). This happened on both operators and for IP, IPV6 and IPV4V6.
- **T-Mobile PL:** VoLTE has to be enabled per number. You send SMS `TAK` to `80535` and get a confirmation SMS ("Usługa VoLTE została aktywowana") within about a day. Prepaid plans like tuBiedronka/Heyah01 are excluded. Once it was active, `AT+CGACT=1,2` → `OK` and the IMS PDN got an IP address. **#33 meant "not subscribed", not "device blocked".**
- **Orange PL:** still #33 on the tested SIM. Orange says VoLTE is free and on by default for compatible devices, but it may gate uncertified devices. This is untested beyond #33.

### 2.2 IMS PDN up is not IMS registered

- After `AT+CGACT=1,2` → `OK`, calls still went through CSFB.
- This firmware has no AT command for the IMS registration state: `AT+CIREG`, `+CAVIMS` and `+CEVDP` all return `ERROR`.
- The state can be read over **QMI** (service IMSA) instead, with `python -m telaius.qmi status`. That showed `NOT_REGISTERED`, with VoIP and SMS-over-IMS unavailable.

### 2.3 Carrier MBN profile (the fix)

The firmware ships 25 carrier MBN profiles. List them with `python -m telaius.qmi mbn-list`. The default is `ROW_Gen_VoLTE`. Others include `Commercial-DT`, `Commercial-Orange`, `Commercial-EE`, `VF_*`, `TF_Germany_VoLTE`, US/JP/KR/CN carriers, and a platform config `MDM9X07_SS`.

- `python -m telaius.qmi mbn-select <id of Commercial-DT>` (T-Mobile PL belongs to the Deutsche Telekom group). The modem resets.
- After the reset:
  - the APNs change: cid2 becomes `INTERNET.TELEKOM` and cid3 becomes `ims`;
  - IMSA reports `REGISTERED` with `sip:+48…@ims.t-mobile.pl`, VoIP and SMS-over-IMS `full`.
- After a later `AT+CRESET`, the modem **auto-selected `ROW_Gen_VoLTE` again** (MBN auto-selection by SIM), but **IMS stayed registered**. It is not established whether the DT profile left IMS settings behind or whether the operator-side activation simply finished propagating.
- To roll back, select `ROW_Gen_VoLTE` (`c9b1b621f64754531bd6b8dad4590e53466cd2f3`) the same way.

With IMS registered, a call stays on LTE: `+CPSI: LTE,Online,...,EUTRAN-BAND3` during the call, `VOICE CALL: BEGIN` about 5–8 s after `ATD`.

### 2.4 Things tried that had no effect

- `AT+CEMODE=1`: returns "operation not supported" with the radio on, `ERROR` with CFUN=0. The value stays at 2.
- `AT+CNV=/nv/item_files/modem/mmode/ue_usage_setting,1,01,1` (Waveshare recipe) + reset: no visible effect. It is still set to 1. The rollback is the same command with `0`.
- `AT+CNV` syntax is undocumented: Waveshare uses `path,value,01,1`. Don't use it to "read" items. `AT+CNVR=?` only covers legacy NV numbers 0–7232.

## 3. VoLTE call audio

### 3.1 USB PCM (`AT+CPCMREG`) does not carry VoLTE on B03

During a VoLTE call, `AT+CPCMREG=1` → `OK` and `AT+CPCMREG?` → `1`, but if4 delivers 0 bytes and writes time out. This is the same at 8 and 16 kHz, with both MBN profiles, and with the undocumented `AT+CUSBAUDIO=1` / `AT+CPCMBANDWIDTH=1,1`, which are not persistent anyway. `CPCMREG=1` before `ATD` returns `ERROR`.

The cause, found over ADB:

- The module's `/usr/bin/alsa_daemon` implements USB audio with Qualcomm host-PCM (HPCM). For `CPCMREG=1` it opens `hw:0,23` / `hw:0,26` (VoiceMMode1 HOST RX CAPTURE / TX PLAYBACK).
- The VoLTE call runs in the VoLTE voice session (FE `hw:0,4`), so the kernel logs `hpcm_get_sess_name: Invalid sess_index` and `voice_get_session: Invalid session_id : 0`, and no audio flows.
- Setting `HPCM_VoLTE tappoint direction samplerate` by hand and using `hw:0,18` / `hw:0,21` (VOLTE HOST RX CAPTURE / TX PLAYBACK) also fails with EIO.

The **analog** path still works on VoLTE. With `CPCMREG=0` the far end hears the onboard microphone through the NAU88C10 codec.

### 3.2 Working path: incall record + incall music via ADB

Run on the module (root shell over ADB) during the call. This was tested on B03 on a real VoLTE call; the far end heard the injected tone, and the far-end voice was recorded.

```sh
# Far end -> host (record the downlink)
amix "MultiMedia1 Mixer VOC_REC_DL" 1
arec -D hw:0,0 -R 16000 -C 1 -F S16_LE -T 12 /tmp/rx.wav     # 16000 or 48000; 8000 fails with "error5"

# Host -> far end (inject into the uplink)
amix "Incall_Music Audio Mixer MultiMedia2" 1
aplay -D hw:0,13 /tmp/tone8k.wav                              # 8 kHz mono WAV worked

# Afterwards
amix "MultiMedia1 Mixer VOC_REC_DL" 0
amix "Incall_Music Audio Mixer MultiMedia2" 0
```

- The recorded downlink has energy up to about 7 kHz (8.6% of the energy in 3.4–7 kHz), so the call ran on **AMR-WB wideband**. On GSM nothing was above 3.4 kHz.
- `VOC_REC_UL` (our own uplink) is also available for MultiMedia1/4/8.
- The kernel logs `voc_start_playback: Invalid session` when incall music stops. This is harmless.
- Both routes are **digital, inside the ADSP voice stream**: the recording is taken after the AMR-WB decoder, and the injected PCM is mixed in before the encoder. Nothing goes through the analog codec. The analog path stays active **in parallel**: the onboard mic (NAU88C10 ADC → I2S → DSP) is summed into the same uplink, and the onboard speaker plays the far end. The mic has to be muted (open: mixer route or `AT+CMICGAIN`).

### 3.3 Transport plan: relay on the module over if4

On the module side, USB **if4 is `/dev/ttyGS0`**. It is the only tty transport of the gadget serial function (`diag,serial,rmnet,ffs`); the other serial interfaces are SMD-backed straight to the modem processor. `alsa_daemon` writes USB audio there for `CPCMREG=1`.

So a small relay on the module can bridge the working ALSA routes to if4:

- far end: `arec hw:0,0` (VOC_REC_DL) → `/dev/ttyGS0`
- host: `/dev/ttyGS0` → `aplay hw:0,13` (Incall_Music)

The host keeps using if4 as raw s16le PCM, so `voicebridge` stays the same apart from the rate (16 kHz, because 8 kHz capture fails). ADB is then only needed to install and start the relay, or to set it to autostart, and can be switched off afterwards. Not built yet. Open questions: buffering and latency, whether `alsa_daemon` must stay out of the way (no `CPCMREG=1`), and when to start/stop the relay (call state from QMI VOICE or AT URCs).

### 3.4 Enabling ADB on the module

```text
AT+CUSBADB=1     # persistent; takes effect after AT+CRESET
AT+CUSBADB=0     # to disable again
```

- With ADB on, the device keeps `1e0e:9001` and adds **if6** (class `ff/42/01`). if0–if5 keep their numbers, so `telaius.modem` (if2 AT, if4 PCM) and `telaius.qmi` (if5) are unaffected.
- `adb` from the Android SDK works on macOS without drivers.
- The shell is root on `mdm9607`: Linux 3.18.44, busybox, tinyalsa tools `amix`/`arec`/`aplay` in `/usr/bin`, syslog in `/var/log/messages`, ALSA card `mdm9607-empoweraudio-i2s-snd-card`.
- Useful ALSA devices (`/proc/asound/pcm`):

| Device | Name | Notes |
|---|---|---|
| `hw:0,0` | MultiMedia1 | capture with `VOC_REC_DL/UL` = call recording |
| `hw:0,2` | CS-Voice | 2G/CSFB voice session FE |
| `hw:0,4` | VoLTE | VoLTE voice session FE (RUNNING all the time) |
| `hw:0,9..12` | CS-VOICE HOST RX/TX CAPTURE/PLAYBACK | HPCM for CS calls (used by `CPCMREG` on 2G) |
| `hw:0,13` | MultiMedia2 | playback with `Incall_Music` = inject into the call |
| `hw:0,18..21` | VOLTE HOST RX/TX CAPTURE/PLAYBACK | HPCM for VoLTE: EIO on B03; used by `alsa_daemon` on B04+ |
| `hw:0,23..26` | VoiceMMode1 HOST … | what B03's `alsa_daemon` opens for `CPCMREG=1` |

### 3.5 USB PCM on VoLTE with firmware B06

After flashing B06, `alsa_daemon` opens the VOLTE HOST devices (`hw:0,18` capture, `hw:0,21` playback), and USB audio on VoLTE started to work, but not reliably. Test calls on 2026-10-25, all VoLTE on T-Mobile PL (`+CPSI: LTE` during the call):

| Time | Tool | Rate | Far end → host (if4 IN) | Host → far end (if4 OUT) | Notes |
|---|---|---|---|---|---|
| 11:49 | test script | 8 kHz | ✅ 16 kB/s | ✅ 50 frames/s, 0 errors | far end heard the test tone |
| 11:51 | voicebridge | 8 kHz | ❌ 0 bytes | ❌ | if4 pumping started ~3 s after `CPCMREG=1` |
| 11:52 | voicebridge | 8 kHz | ✅ | ✅ | pumping started before `CPCMREG=1`; 45 s conversation with Grok |
| 11:54 | voicebridge | 16 kHz | ❌ 0.1 s | ❌ | `CPCMFRM=1` set at runtime after earlier 8 kHz calls |
| 11:55 | test script | 16 kHz | ✅ 32 kB/s | ✅ 0 errors | `CPCMFRM=1` right after reset; stream started with a stray byte |
| 11:59–12:04 (4 calls) | voicebridge | 16 / 8 kHz | ✅ (Grok heard and answered the callee) | ❌ writes time out, far end hears nothing | after each call `pcm21p` stays `PREPARED` |

What was tried:

- **Start pumping if4 before `CPCMREG=1`.** B06 drops the call's USB audio if the host does not read and write right away. This fixed the 11:51 failure.
- **Stream alignment.** At 16 kHz the IN stream can start with one stray byte (first read was 2015 bytes), which turns every sample into noise. Realigned, the recording is proper AMR-WB speech: 8.6% of the energy in 3.4–7 kHz, 1.4% above 7 kHz. `telaius.modem.PcmAligner` picks the offset from the first 0.3 s.
- **Re-arm watchdog.** If no audio arrives within 1.5 s, send `CPCMREG=0` → `CPCMREG=1` (up to 3 times). It does not fix a dead uplink.
- **No `CPCMREG=0` before dialing.** No change.
- **Restarting `alsa_daemon`** (`kill`; init respawns it from `/etc/inittab`). This clears the stuck `pcm21p`, but the next call failed the same way. A restart also resets `CPCMFRM` to 0.
- **`CPCMFRM=1` set at runtime** after earlier calls did not take effect reliably. Set it right after boot.

Diagnostics:

- `alsa_daemon` writes its stdout to `/dev/tty0`; read it with `adb shell cat /dev/vcs1` and clear it with `printf "\033c" > /dev/tty1`.
- The `Arec: error5` lines there come from the DTMF detector (`pcm8c`) and also appear when idle, so they are not USB audio.
- Open PCM streams: `/proc/asound/card0/pcm*/sub0/status`. During a failed uplink, `pcm21p` sits in `PREPARED`, meaning the daemon never gets data from `/dev/ttyGS0` to start playback.

Conclusion: on B06 the uplink (host → callee) over `CPCMREG` is unreliable. This matches [LilyGO issue #542](https://github.com/Xinyuan-LilyGO/LilyGo-Modem-Series/issues/542) (B06: downlink OK, uplink silent). The next step is the relay from section 3.3 for at least the uplink (incall music), with `alsa_daemon` kept out of the call.

### 3.6 SIMCom hotfix `LE20B06SIM7600G22_260714`: uplink fixed at 8 kHz

LilyGO staff posted the hotfix in [issue #542](https://github.com/Xinyuan-LilyGO/LilyGo-Modem-Series/issues/542) on 2026-10-20 ("SIMCom has provided a new firmware that you can test"). It ships without release notes. Compared with B06:

| Part | Change |
|---|---|
| Kernel (`boot`, built 2026-07-14) | the `hpcm_start_vocpcm` check (`Invalid hpcm start request`) added in B06 is gone; the DT table differs |
| `alsa_daemon` (`system`) | reverted to the B05 code: mmap-based USB audio playback (`mmap_buffer`, `sync_ptr`, `pcm_avail`), taps fixed at 8000 Hz, `libamrnb` instead of fdk-aac/opencore |
| `modem` | same base (`MPSS_JO_2_0_1.04.01.07.03.09`), segments rebuilt |
| `appsboot` | differs |
| `sbl`, `tz`, `rpm`, partition table | byte-identical to B06 |

Tests:

1. **Hotfix kernel + B06 system** (16 kHz `alsa_daemon`), flashed `boot` only: 3 calls at 16 kHz. Call 1 OK both ways (first call after boot); calls 2 and 3 had a dead uplink, `pcm21p` stuck `PREPARED`, even after restarting `alsa_daemon`. **The kernel alone does not fix it**; the fix is in the reverted `alsa_daemon`.
2. **Full hotfix**: 3 consecutive `voicebridge` calls at 8 kHz, all OK both ways. TX audio written equals call length (21.9/21.9 s, 17.9/17.9 s, 57.8/57.8 s), the far end heard the bot clearly, response latency 0.5–0.9 s (with some outliers of exactly 3.00 s still under investigation).

`fastboot boot boot.img` (RAM boot) is not usable on this module: aboot answers `dtb not found`, even for the stock B06 image, so testing a kernel means flashing `boot`.

### 3.7 Relay feasibility on the hotfix kernel

Tested over ADB with no `CPCMREG`, using the routes from section 3.2:

- `Incall_Music` playback on `hw:0,13` runs at **16000 Hz**. The far end heard the 16 kHz beeps and tone.
- `VOC_REC_DL` capture on `hw:0,0` records fully at 16000 and 48000 Hz.
- The same phrase recorded three ways (16 kHz, 16 kHz mmap, 48 kHz) gave 200 / 300 / 24 ms of digital-silence gaps and 3.7 / 6.3 / 5.8 % of energy in 3.4–7 kHz. No audible difference. The relay will capture at 48 kHz and resample to 16 kHz.
- The codec seems to vary per call: one call looked narrowband (AMR-NB). The relay should log the negotiated codec (QMI VOICE).

### 3.8 telaius-relay (built and working)

`relay/telaius-relay.c` is a static ARMv7 binary (about 470 KB), cross-built in Docker (`relay/Dockerfile`, `relay/Makefile`). It runs on the module in place of `CPCMREG`:

```
host if4 OUT → /dev/ttyGS0 → jitter buffer (60 ms target) → hw:0,13 @16 kHz + Incall_Music → uplink
downlink → VOC_REC_DL + hw:0,0 @48 kHz → FIR 48→16 kHz → /dev/ttyGS0 → host if4 IN
```

- **Session control is implicit.** Frames arriving on `ttyGS0` start a session (mixers on, PCMs open); 1.5 s without frames stops it. With no call, capture returns EIO, and the relay retries every 300 ms.
- **Board mic mute (`-m`).** On session start it sets `SIMCOM Switch Device` to `Sleep`, so the NAU8810 codec stops feeding the uplink while AUX PCM and the DSP TX path stay up. Removing `VoLTE_Tx Mixer AUX_PCM_TX_VoLTE` instead also silenced incall music, so the far end heard nothing.
- **PCM periods must be small:** capture 160 frames × 8, playback 64 frames × 8 (what `arec`/`aplay` use). A 20 ms period was accepted by `hw_params`, but capture then failed with EIO every ~2 s. Transfers are still 20 ms.
- **`-l` loopback** echoes `ttyGS0` back to the host. `loop_test`-style check: 96000 B sent, 96000 B returned, byte-exact, 0 errors.
- **`-v`** logs stats every 5 s. The log goes to `/tmp/relay.log` on the module.
- **Install:** `relay/install.sh` (over ADB). It pushes the binary to `/data/telaius/` and adds an init respawn entry `tr:2345:respawn:/data/telaius/run.sh` to `/etc/inittab` (root is remounted rw for that; backup at `/data/telaius/inittab.orig`). `--remove` undoes the entry. Flashing `system` drops it.
- Detached start without init: `start-stop-daemon -S -b -x /data/telaius/run.sh`. A background `&` inside `adb shell` dies with the session. Make sure only one instance holds `ttyGS0`.
- Verified: after `AT+CRESET` init starts the relay within the first second of boot (`ppid=1`).
- The module's old `adbd` has two quirks. `adb shell 'cat > f' <<EOF` never delivers EOF, so the remote `cat` hangs; push files instead. `adb shell` does not pass exit codes, so scripts must check output, not `$?`.

Results on the hotfix firmware (2026-10-25, VoLTE T-Mobile PL, Grok voice Rex):

| Call | Duration | Uplink (host → far end) | Downlink (far end → host) | Notes |
|---|---|---|---|---|
| with `-m` = drop AUX_PCM_TX route | 30 s | ❌ silent | ❌ capture EIO | wrong mute method + 20 ms periods |
| no mute | 25 s | ✅ heard bot + board mic | ❌ capture EIO | 20 ms periods |
| no mute, small periods | 75 s | ✅ | ✅ 32 kB/s, 0 xruns, 4.5 % energy 3.4–7 kHz | board mic audible to the far end |
| `-m` = codec Sleep | 45 s | ✅ | ✅ 0 xruns, 0 capture errors | board mic gone; latency 0.7–0.85 s |

Open:
- The audio Grok sends at 16 kHz has only ~0.6 % of its energy above 3.4 kHz. Check whether xAI output is band-limited at 16 kHz (try 24 kHz and resample).
- Log the negotiated codec (AMR-WB vs AMR-NB) per call.

## 4. QMI over raw libusb (macOS)

`telaius/qmi.py` speaks QMI on **if5** (class ff/ff/ff, interrupt EP 0x8b) through CDC control transfers: `SEND_ENCAPSULATED_COMMAND` 0x21/0x00 and `GET_ENCAPSULATED_RESPONSE` 0xA1/0x01. Gotchas:

- QMI is silent until **DTR is asserted on if5** (`SET_CONTROL_LINE_STATE`), the same quirk the Linux `qmi_wwan` driver applies.
- After a modem reset, CTL may stay silent until a **CTL Sync (0x0027)** request succeeds, sometimes only after retries.
- On B06, the RESPONSE_AVAILABLE notifications on the interrupt endpoint 0x8b must be drained; otherwise responses stop after the first one.
- Supported services on this firmware include NAS 0x03, VOICE 0x09, UIM 0x0B, IMS settings 0x12, IMSP 0x1F, IMSA 0x21, PDC 0x24 and DSD 0x2A.

## 5. Firmware updates (done 2026-10-25)

The module was flashed from `LE20B03SIM7600M22` to `LE20B06SIM7600G22`, then to the hotfix `LE20B06SIM7600G22_260714`, all from macOS without Windows. The procedure below is the B06 flash. The hotfix was flashed the same way: `aboot`, `modem`, `boot`, `system`, `recovery`, `recoveryfs`; `sbl`/`tz`/`rpm` are identical and were skipped. After the hotfix: `AT+CGMR` → `LE20B06SIM7600G22_260714`, IMEI unchanged, IMS registered, `CUSBADB` reset to 0.

### 5.0 Procedure as performed

1. **Backup.** With ADB on, each partition the fastboot set touches was dumped with `adb exec-out "cat /dev/mtdN" > name.bin`: `sbl` (mtd0), `tz` (3), `rpm` (4), `aboot` (5), `boot` (6), `modem` (9), `recovery` (11), `recoveryfs` (13), `system` (14). Each partition was read twice and the SHA-256 checksums matched. Headers checked: `ANDROID!` for boot/recovery, `UBI#` for modem/system/recoveryfs. The dump is kept outside git.
   - Reading `mibib` (mtd1) hung for about 13 minutes and dropped the modem off USB (fixed by a power cycle). `efs2` (mtd2, calibration and IMEI) does not return data. Neither is touched by fastboot.
   - Restoring from these raw dumps is untested.
2. **Check the package.** The B06 `partition_nand.xml` matches the module's `/proc/mtd` layout, and every image fits its partition. `partition.mbn` is identical to B04's. Fastboot does not rewrite the partition table.
3. **Enter fastboot.** `AT+BOOTLDR` only rebooted into normal mode. `adb reboot bootloader` (with `AT+CUSBADB=1`) worked: `fastboot devices` → `MDM9607 fastboot` (USB `18d1:d00d`).
4. **Flash**, one partition at a time, stopping on the first error. All returned `OKAY` (about 40 s in total):
   ```text
   fastboot flash aboot appsboot.mbn
   fastboot flash rpm rpm.mbn
   fastboot flash sbl sbl1.mbn
   fastboot flash tz tz.mbn
   fastboot flash modem modem.img
   fastboot flash boot boot.img
   fastboot flash system system.img
   fastboot flash recovery recovery.img
   fastboot flash recoveryfs recoveryfs.img
   fastboot reboot
   ```
5. **After boot:**
   - `AT+CGMR` → `LE20B06SIM7600G22`; IMEI unchanged; SIM `READY`; LTE registered; `VOLTESETTING` 1.
   - `CUSBADB` was reset to 0, so ADB has to be re-enabled.
   - USB layout unchanged (if0–if5).
   - IMS `REGISTERED` immediately, with `ROW_Gen_VoLTE` active.

Windows/QDL would only be needed for emergency (EDL) recovery if a bootloader flash failed. The firmware must match the module P/N.

### 5.1 Offline comparison of builds (before flashing)

The tested module is P/N `S2-1097D-Z307J` (R2). LilyGO maps `S2-1097D-*` to the `LE20B0xSIM7600G22` builds. The factory `LE20B03SIM7600M22` appears to be the same LE20 (G-H) line under the older suffix; the LE11…M22 line is for the SIM7600E-H.

Each build's `system.img` (UBI) was unpacked with `ubi_reader` and `/usr/bin/alsa_daemon` was compared:

| Build | Kernel build date | Devices `alsa_daemon` opens for USB audio | HPCM tap rate |
|---|---|---|---|
| B03 M22 (factory) | 2020-08-28 | `hw:0,23/25/26` (VoiceMMode1: wrong session for VoLTE) | fixed 8000 |
| B04 M22 | 2021-05-27 | `hw:0,18/20/21` (VOLTE HOST) | fixed 8000 |
| B05 G22 | 2024-01-12 | `hw:0,18/20/21` | fixed 8000 |
| B06 G22 | 2025-06-24 | `hw:0,18/20/21` | **parameterized (`%d`)** → 16 kHz possible, uplink unreliable |
| B06 hotfix `_260714` (installed) | 2026-07-14 | `hw:0,18/20/21` | fixed 8000 (B05 code), uplink works |

- From B04 on, the USB audio path targets the VoLTE HPCM devices, which is exactly the B03 bug.
- B06 also adds AMR-WB/AMR-NB codec libraries and AUX PCM rate switching. Its kernel adds a new check in `hpcm_start_vocpcm` (`Invalid hpcm start request`).
- Before flashing this was only an expectation; section 3.5 has the results. [LilyGO issue #542](https://github.com/Xinyuan-LilyGO/LilyGo-Modem-Series/issues/542) reports one-way USB PCM (uplink silent) on B06.
- Other packages: [AiTechOne/SIM7600_Firmware_Upgrade](https://github.com/AiTechOne/SIM7600_Firmware_Upgrade) (B04 M22, unofficial); Techship distributes official archives.
- Packages: [LilyGO update guide](https://github.com/Xinyuan-LilyGO/LilyGo-Modem-Series/blob/main/docs/en/esp32/sim7600-esp32/upgrade/sim7600_upgrade.md) (Google Drive links for B04/B05/B06 G22; RAR with the same fastboot image set as B04).

## References

- [Waveshare SIM7600G-H wiki](https://www.waveshare.com/wiki/SIM7600G-H_4G_Module) (VoLTE `ue_usage_setting` recipe)
- [SIMCom USB Audio Application Note V2.00](https://m5stack.oss-cn-shenzhen.aliyuncs.com/resource/docs/datasheet/module/sim7600x/SIM7100_SIM7500_SIM7600_SIM7800%20Series_USB%20AUDIO_Application%20Note_V2.00.pdf)
- [LilyGO issue #542](https://github.com/Xinyuan-LilyGO/LilyGo-Modem-Series/issues/542) (one-way USB PCM on G22 firmware)
- [T-Mobile PL VoLTE & VoWiFi](https://www.t-mobile.pl/c/volte-i-wifi)
