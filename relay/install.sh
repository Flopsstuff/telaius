#!/bin/sh
# Install telaius-relay on the SIM7600 module over ADB (needs AT+CUSBADB=1 + reset) and make init
# start it at boot. Re-running is safe. Flashing the module's system partition undoes the inittab entry.
#
#   relay/install.sh            install / update and (re)start
#   relay/install.sh --remove   stop it and remove the inittab entry (binary stays in /data/telaius)
set -eu
cd "$(dirname "$0")"
ENTRY='tr:2345:respawn:/data/telaius/run.sh'

adb get-state >/dev/null

if [ "${1:-}" = "--remove" ]; then
    adb shell "mount -o remount,rw / && sed -i '\|^$ENTRY\$|d' /etc/inittab; sync; mount -o remount,ro /; kill -HUP 1; killall telaius-relay 2>/dev/null; true"
    echo "removed inittab entry, relay stopped"
    exit 0
fi

[ -x telaius-relay ] || docker run --rm -v "$PWD":/src telaius-armbuild make
adb shell 'mkdir -p /data/telaius'
adb push telaius-relay /data/telaius/telaius-relay.new >/dev/null
adb shell 'chmod 755 /data/telaius/telaius-relay.new && mv /data/telaius/telaius-relay.new /data/telaius/telaius-relay'
# adb push, not `adb shell 'cat >' <<EOF`: the module's old adbd never delivers EOF, so cat would hang.
adb push run.sh /data/telaius/run.sh >/dev/null
adb shell 'chmod 755 /data/telaius/run.sh'

# The old adbd does not pass exit codes through `adb shell`, so test its output instead.
if [ "$(adb shell "grep -c '^$ENTRY\$' /etc/inittab" | tr -d '\r')" = "0" ]; then
    adb shell "[ -f /data/telaius/inittab.orig ] || cp /etc/inittab /data/telaius/inittab.orig"
    adb shell "mount -o remount,rw / && echo '$ENTRY' >> /etc/inittab; sync; mount -o remount,ro /"
    echo "added inittab entry (backup: /data/telaius/inittab.orig)"
fi
# Let init (re)start the new binary: stop any manual instance, then make init re-read inittab.
pid=$(adb shell 'killall telaius-relay 2>/dev/null; kill -HUP 1; sleep 2; pidof telaius-relay' | tr -d '\r')
[ -n "$pid" ] && echo "telaius-relay running (pid $pid)" || { echo "telaius-relay is NOT running; see /tmp/relay.log on the module"; exit 1; }
