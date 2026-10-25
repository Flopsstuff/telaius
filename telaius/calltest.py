"""M0 audio test: dial, switch call audio to USB PCM, play a WAV into the call and record the far end.

Usage: python -m telaius.calltest NUMBER PLAY.wav OUT.wav [--seconds N]

PLAY.wav must be 16-bit mono at the modem's current PCM rate (8000 or 16000 Hz), e.g.:
  say -v Milena -o phrase.aiff "Привет!" && afconvert -f WAVE -d LEI16@8000 -c 1 phrase.aiff phrase.wav
"""
import argparse
import sys
import threading
import time
import wave

import usb.core

from telaius.modem import IF_AT, IF_PCM, AtChannel, UsbPort, close_modem, log, open_modem


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("number")
    ap.add_argument("play")
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=20)
    a = ap.parse_args()

    dev = open_modem()
    at = AtChannel(UsbPort(dev, IF_AT))
    pcm = UsbPort(dev, IF_PCM)
    rate = at.pcm_rate()
    bps = rate * 2
    frame_len = bps // 50  # 20 ms, 16-bit mono

    with wave.open(a.play) as w:
        if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (rate, 2, 1):
            close_modem(dev, [IF_AT, IF_PCM])
            sys.exit(f"{a.play} must be {rate} Hz / 16-bit / mono (modem PCM rate is {rate} Hz)")
        tx = w.readframes(w.getnframes())

    rx = bytearray()
    stats = {"rx_reads": 0, "tx_frames": 0, "tx_err": 0}
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            chunk = pcm.read()
            if chunk:
                rx.extend(chunk)
                stats["rx_reads"] += 1

    def writer():
        # Phrase, 1 s silence, phrase again, then silence; paced at real time.
        stream = tx + bytes(bps) + tx
        pos, t0 = 0, time.monotonic()
        while not stop.is_set():
            frame = stream[pos:pos + frame_len]
            frame += bytes(frame_len - len(frame))
            pos += frame_len
            try:
                pcm.write(frame, timeout_ms=200)
                stats["tx_frames"] += 1
            except usb.core.USBError:
                stats["tx_err"] += 1
            t0 += 0.02
            delay = t0 - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    try:
        at.send("ATE0")
        at.send("AT+CPCMREG=0")
        at.send(f"ATD{a.number};", wait=10)
        got = at.wait_for(["VOICE CALL: BEGIN", "NO CARRIER", "BUSY", "NO ANSWER", "VOICE CALL: END"], 60)
        if got != "VOICE CALL: BEGIN":
            log(f"call not answered: {got}")
            return
        # Audio can only be moved to USB once the call is active.
        at.send("AT+CPCMREG=1")
        at.send("AT+CPSI?")
        threads = [threading.Thread(target=reader, daemon=True), threading.Thread(target=writer, daemon=True)]
        for t in threads:
            t.start()
        end = time.time() + a.seconds
        while time.time() < end:
            at.poll(200)
            if "VOICE CALL: END" in at.buf or "NO CARRIER" in at.buf:
                log("remote hung up")
                break
        stop.set()
        for t in threads:
            t.join(1)
    finally:
        stop.set()
        at.send("AT+CHUP")
        at.send("AT+CPCMREG=0")
        with wave.open(a.out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(rx))
        log(f"saved {len(rx) / bps:.1f}s to {a.out}; stats={stats}")
        close_modem(dev, [IF_AT, IF_PCM])


if __name__ == "__main__":
    main()
