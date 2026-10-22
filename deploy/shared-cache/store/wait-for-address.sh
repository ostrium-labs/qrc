#!/bin/sh
# ExecStartPre for rustfs.service: wait until the tailnet address in RUSTFS_ADDRESS is
# assigned to an interface, so RustFS binds to it rather than failing early at boot.
# Gives up after 60 seconds; systemd then retries the unit (Restart=on-failure).
set -eu
ip="${RUSTFS_ADDRESS%:*}"
i=0
while [ "$i" -lt 60 ]; do
  if ip -o addr show | grep -qF " $ip/"; then
    exit 0
  fi
  i=$((i + 1))
  sleep 1
done
echo "the tailnet address $ip is not up after 60 s; is tailscaled running?" >&2
exit 1
