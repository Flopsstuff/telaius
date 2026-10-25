#!/bin/sh
# Started by init (inittab entry "tr"), restarted if it exits. Log: /tmp/relay.log (tmpfs).
exec /data/telaius/telaius-relay -m >> /tmp/relay.log 2>&1
