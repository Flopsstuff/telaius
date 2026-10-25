"""Minimal AT console for the SIM7600.

Usage: python -m telaius.at [--wait SECONDS] CMD [CMD ...]
Each CMD is sent in order and its output printed. "sleep:N" waits N seconds while
printing unsolicited result codes (e.g. during a call).
"""
import argparse
import sys
import time

from telaius.modem import IF_AT, AtChannel, close_modem, open_modem, open_port


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wait", type=float, default=5, help="max seconds to wait for each command's result")
    ap.add_argument("cmds", nargs="+")
    a = ap.parse_args()

    dev = open_modem()
    try:
        at = AtChannel(open_port(dev, IF_AT), echo=False)
        at.poll(300)
        sys.stdout.write(at.buf)
        for cmd in a.cmds:
            if cmd.startswith("sleep:"):
                print(f"\n--- waiting {cmd[6:]}s ---")
                end = time.time() + float(cmd[6:])
                while time.time() < end:
                    sys.stdout.write(at.poll())
                    sys.stdout.flush()
                continue
            print(f"\n>>> {cmd}")
            sys.stdout.write(at.send(cmd, a.wait))
            sys.stdout.flush()
    finally:
        close_modem(dev, [IF_AT])


if __name__ == "__main__":
    main()
