#!/usr/bin/env bash
# Flash the prebuilt Wio Terminal firmware (.bin) using arduino-cli + bossac.
# Usage: tools/flash_wio.sh [PORT]
#   PORT is optional. If omitted, the script auto-detects the Wio Terminal
#   (Seeed VID 0x2886, PIDs 0x802D / 0x002D) from the serial ports.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$REPO/arduino/ai_quota_dashboard/build/ai_quota_dashboard.ino.bin"
FQBN="Seeeduino:samd:seeed_wio_terminal"

if [[ ! -f "$BIN" ]]; then
  echo "ERROR: firmware not found: $BIN" >&2
  exit 1
fi

PORT="${1:-}"
if [[ -z "$PORT" ]]; then
  # Prefer a port arduino-cli recognizes as a Seeed/Wio board, else the first
  # non-Bluetooth serial port (the freshly-plugged Wio Terminal).
  PORT="$(arduino-cli board list 2>/dev/null \
    | awk '$1 ~ /^\/dev\/cu\./ && ($0 ~ /Seeeduino|Wio/ || $0 ~ /Unknown/) && $1 !~ /Bluetooth/ {print $1; exit}')"
fi

if [[ -z "$PORT" ]]; then
  echo "ERROR: no Wio Terminal port detected. Plug it in with a DATA USB cable, power switch ON, then retry." >&2
  arduino-cli board list 2>&1
  exit 1
fi

echo "==> Board:  $FQBN"
echo "==> Port:   $PORT"
echo "==> Firmware: $(shasum -a 256 "$BIN" | cut -d' ' -f1) ($(stat -f%z "$BIN") bytes)"
echo "==> Uploading..."
arduino-cli upload -p "$PORT" -b "$FQBN" --input-file "$BIN" --verbose 2>&1 | tail -20
