#!/usr/bin/env bash
#
# get_dbc.sh — downloads the Toyota DBC files into dbc/
# ---------------------------------------------------------------------------
# The DBCs come from commaai/opendbc, at exactly the revision that
# frk2/toyoyta_radar_control_can pulls in as a submodule (fork frk2/opendbc).
# That revision is pinned to a commit and verified via SHA-256 so that
# everyone works with identical signal definitions. The current commaai
# master no longer has the *_prius_2017_* files in this form.
#
# Usage:   tools/get_dbc.sh
# Target:  dbc/  (gitignored; other location via RADAR_DBC_DIR, see radar_dbc.py)
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
DEST="${RADAR_DBC_DIR:-$REPO_DIR/dbc}"
COMMIT="119ea01bf60a8b186ccacae44fadec53620a2f44"
BASE="https://raw.githubusercontent.com/frk2/opendbc/$COMMIT"

FILES=(
  "toyota_prius_2017_adas.dbc          f13f9e3d396011d7fb51b2dd1713221f2849cbb969ce56576289db30cedc51eb"
  "toyota_prius_2017_pt_generated.dbc  d5802ee75e40d43f7fda586dffa56aa0bd84ee26fb130a0bf45a34d00a50e576"
)

mkdir -p "$DEST"
for entry in "${FILES[@]}"; do
  read -r name sha <<< "$entry"
  out="$DEST/$name"
  if [[ -f "$out" ]] && echo "$sha  $out" | sha256sum -c --quiet 2>/dev/null; then
    echo "[get_dbc] $name already present and verified."
    continue
  fi
  echo "[get_dbc] Downloading $name …"
  curl -fsSL -o "$out.tmp" "$BASE/$name"
  if ! echo "$sha  $out.tmp" | sha256sum -c --quiet; then
    rm -f "$out.tmp"
    echo "[get_dbc] ERROR: checksum of $name does not match." >&2
    exit 1
  fi
  mv "$out.tmp" "$out"
done
echo "[get_dbc] Done: $DEST"
