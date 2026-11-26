#!/usr/bin/env bash
#
# get_dbc.sh — downloads the Toyota DBC files into dbc/
# ---------------------------------------------------------------------------
# Two sources, each pinned to a commit and verified via SHA-256 so that
# everyone works with identical signal definitions:
#
#  - toyota_prius_2017_*.dbc from frk2/opendbc (a fork of commaai/opendbc), at
#    exactly the revision that frk2/toyoyta_radar_control_can pulls in as a
#    submodule. The tools decode with these; spoof_dsu.py builds its SPEED
#    frame from the PT file. The current commaai master no longer has the
#    *_prius_2017_* files in this form.
#  - toyota_adas.dbc from commaai/opendbc (state of June 2026). LONG_DIST
#    in 0.01 m steps instead of 0.04 m, plus SCORE in TRACK_B and
#    STATUS_MSG (0x123), see docs/radar-frames.md. Not used by the tools
#    yet; for decoding by hand: radar_dbc.load(radar_dbc.ADAS_CURRENT).
#
# Usage:   tools/get_dbc.sh
# Target:  dbc/  (gitignored; other location via RADAR_DBC_DIR, see radar_dbc.py)
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
DEST="${RADAR_DBC_DIR:-$REPO_DIR/dbc}"
FRK2="https://raw.githubusercontent.com/frk2/opendbc/119ea01bf60a8b186ccacae44fadec53620a2f44"
COMMA="https://raw.githubusercontent.com/commaai/opendbc/75889fd9283730feb6ec824a00975fda0807bbc5/opendbc/dbc"

FILES=(  # name  sha256  base URL
  "toyota_prius_2017_adas.dbc          f13f9e3d396011d7fb51b2dd1713221f2849cbb969ce56576289db30cedc51eb  $FRK2"
  "toyota_prius_2017_pt_generated.dbc  d5802ee75e40d43f7fda586dffa56aa0bd84ee26fb130a0bf45a34d00a50e576  $FRK2"
  "toyota_adas.dbc                     e50d02305cd62226b67d6dcd9f681b98def712eddc7929bd0011cfb1b4dae45c  $COMMA"
)

mkdir -p "$DEST"
for entry in "${FILES[@]}"; do
  read -r name sha base <<< "$entry"
  out="$DEST/$name"
  if [[ -f "$out" ]] && echo "$sha  $out" | sha256sum -c --quiet 2>/dev/null; then
    echo "[get_dbc] $name already present and verified."
    continue
  fi
  echo "[get_dbc] Downloading $name …"
  curl -fsSL -o "$out.tmp" "$base/$name"
  if ! echo "$sha  $out.tmp" | sha256sum -c --quiet; then
    rm -f "$out.tmp"
    echo "[get_dbc] ERROR: checksum of $name does not match." >&2
    exit 1
  fi
  mv "$out.tmp" "$out"
done
echo "[get_dbc] Done: $DEST"
