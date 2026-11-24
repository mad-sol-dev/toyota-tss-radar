#!/usr/bin/env bash
#
# roadlog.sh — one-command drive recording for the Toyota TSS-P radar
# ---------------------------------------------------------------------------
# Starts the DSU spoof (if needed), logs can_radar + can_car in the
# canonical SocketCAN log format (canplayer-compatible) and — if a GNSS
# receiver is given or unambiguously detected — GNSS via gnsslog.py
# (raw stream + gnss.csv, host-time stamped like candump -L).
# Everything goes into a session folder under logs/. Ctrl+C stops it all cleanly.
#
# Idea: recording and analysis run on the same laptop. During the drive
# this script captures; analysis happens locally afterwards.
#
# Usage:
#   tools/roadlog.sh [LABEL] [--gnss SOURCE] [--no-spoof]
#
#   --gnss reach                 Emlid Reach on USB (/dev/ttyACM0, ERB)
#   --gnss nmea:<dev>[@baud]     serial receiver, e.g. nmea:/dev/ttyUSB0@9600
#   --gnss tcp:<host>:<port>     TCP stream (Reach TCP output, phone gpsdRelay)
#   --gnss none                  no GNSS
#   (no --gnss: the /dev/ttyACM* ports are probed for 3 s each; GNSS is
#    logged only if exactly one of them sends ERB or NMEA)
#
# Examples:
#   tools/roadlog.sh country-road --gnss reach
#   tools/roadlog.sh motorway --gnss tcp:192.168.2.15:9001
#   tools/roadlog.sh standstill-test --no-spoof   # spoof already running elsewhere
#
# Stop: Ctrl+C  (shuts down spoof + loggers cleanly, writes summary)
# ---------------------------------------------------------------------------
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # tools/
REPO_DIR="$(dirname "$SCRIPT_DIR")"
RADAR_IF="can_radar"
CAR_IF="can_car"
# Prefer the project venv (cantools etc.), fall back to the system python.
PY="$REPO_DIR/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

# ── Arguments ───────────────────────────────────────────────────────────────
LABEL=""
GNSS=""
GPS_DEV=""      # deprecated --gps/--baud, mapped to --gnss nmea:<dev>@<baud>
GPS_BAUD="9600"
START_SPOOF=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gnss)     GNSS="$2"; shift 2 ;;
    --gps)      GPS_DEV="$2"; shift 2 ;;
    --baud)     GPS_BAUD="$2"; shift 2 ;;
    --no-spoof) START_SPOOF=0; shift ;;
    -h|--help)  sed -n '2,30p' "$0"; exit 0 ;;
    -*)         echo "[roadlog] unknown option $1 (see --help)" >&2; exit 2 ;;
    *)          LABEL="$1"; shift ;;
  esac
done
[[ -z "$GNSS" && -n "$GPS_DEV" ]] && GNSS="nmea:${GPS_DEV}@${GPS_BAUD}"

# ── GNSS source: as given, or detected only if unambiguous ──────────────────
# Detection reads the data itself (gnsslog.py --probe), not device names.
# Only /dev/ttyACM* is probed: opening a /dev/ttyUSB* port can reset the
# board behind it (ESP32/Arduino); give such receivers explicitly.
GNSS_NOTE=""
if [[ -z "$GNSS" ]]; then
  found=()
  # ROADLOG_PROBE_DEVS overrides the candidate list (tests with a pty).
  for d in ${ROADLOG_PROBE_DEVS:-/dev/ttyACM*}; do
    [[ -e "$d" ]] || continue
    fmt="$("$PY" "$SCRIPT_DIR/gnsslog.py" --probe "$d" 2>/dev/null)" \
      && found+=("$d ($fmt)")
  done
  if [[ ${#found[@]} -eq 1 ]]; then
    GNSS="serial:${found[0]%% *}@57600"
    GNSS_NOTE="auto-detected: ${found[0]}"
  elif [[ ${#found[@]} -gt 1 ]]; then
    GNSS="none"
    GNSS_NOTE="ambiguous, not logged: ${found[*]}"
    echo "[roadlog] WARNING: several GNSS sources (${found[*]}) — choose one with --gnss" >&2
  else
    GNSS="none"
    GNSS_NOTE="none detected"
  fi
fi

# ── Session folder ──────────────────────────────────────────────────────────
TS="$(date +%Y-%m-%d_%H%M%S)"
SESSION="$REPO_DIR/logs/${TS}${LABEL:+_$LABEL}"
mkdir -p "$SESSION"
echo "[roadlog] Session: $SESSION"

# ── Check interfaces ─────────────────────────────────────────────────────────
for IF in "$RADAR_IF" "$CAR_IF"; do
  if ! ip -br link show "$IF" 2>/dev/null | grep -q 'UP'; then
    echo "[roadlog] WARNING: $IF is not UP. Replug the USB hub? (udev then takes over)" >&2
  fi
done

# ── Manifest ─────────────────────────────────────────────────────────────────
EGO_KPH="$(grep -oP 'EGO_SPEED_KPH\s*=\s*\K[0-9.]+' "$SCRIPT_DIR/spoof_dsu.py" 2>/dev/null || echo '?')"
GIT_COMMIT="$(cd "$REPO_DIR" && git rev-parse --short HEAD 2>/dev/null || echo '?')"
{
  echo "# Road session $TS"
  echo
  echo "- **Label:** ${LABEL:-—}"
  echo "- **Start:** $(date -Iseconds)"
  echo "- **Git commit:** $GIT_COMMIT"
  echo "- **EGO_SPEED_KPH (spoof):** $EGO_KPH"
  echo "- **GNSS source:** ${GNSS}${GNSS_NOTE:+ ($GNSS_NOTE)}"
  echo "- **Kernel:** $(uname -r)"
  echo
  echo "## Notes (fill in by hand)"
  echo "- Route: "
  echo "- Weather/visibility: "
  echo "- Anything notable during the drive: "
} > "$SESSION/session.md"

# ── Cleanup trap ─────────────────────────────────────────────────────────────
PIDS=()
cleanup() {
  echo
  echo "[roadlog] Stopping …"
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
  sleep 0.5
  for pid in "${PIDS[@]}"; do kill -9 "$pid" 2>/dev/null; done

  echo "[roadlog] ── Summary ──────────────────────────────"
  echo "  End: $(date -Iseconds)" >> "$SESSION/session.md"
  for f in "$SESSION"/*.log "$SESSION"/*.csv "$SESSION"/*.raw; do
    [[ -e "$f" ]] || continue
    case "$f" in *.raw) lines="-" ;; *) lines="$(wc -l < "$f")" ;; esac
    size="$(du -h "$f" | cut -f1)"
    printf "  %-22s %8s lines  %6s\n" "$(basename "$f")" "$lines" "$size"
  done
  echo "[roadlog] Session saved: $SESSION"
  exit 0
}
trap cleanup INT TERM

# ── Supervisor: keep a logger/spoof running ──────────────────────────────────
# supervise NAME OUTFILE CMD...  runs CMD in the background with stdout+stderr
# appended to OUTFILE and restarts it 2 s after it exits — e.g. candump after
# a USB reset ("No such device" until udev brings the interface back up), or
# the spoof after "No buffer space" while the radar has no power (a restart
# also re-sends its one-time init messages). Every restart is noted in
# restarts.log, which roadstatus.py counts. On TERM the supervisor stops its
# child and exits.
supervise() {
  local name="$1" out="$2"; shift 2
  (
    child=0
    trap 'kill "$child" 2>/dev/null; exit 0' TERM INT
    while true; do
      "$@" >> "$out" 2>&1 &
      child=$!
      wait "$child"; rc=$?
      echo "$(date -Iseconds) $name exited (status $rc), restarting in 2 s" >> "$SESSION/restarts.log"
      sleep 2
    done
  ) &
  PIDS+=("$!")
}

# ── Start spoof (unless already running) ─────────────────────────────────────
if [[ "$START_SPOOF" -eq 1 ]]; then
  # Match only processes that *run* the script (python … spoof_dsu.py or
  # ./spoof_dsu.py), not e.g. an editor or shell that merely mentions it.
  if pgrep -f '^([^ ]*/)?(python[0-9.]* +([^ ]*/)?)?spoof_dsu\.py( |$)' >/dev/null; then
    echo "[roadlog] Spoof already running (pgrep) — not starting a second one."
  else
    echo "[roadlog] Starting DSU spoof (restarted automatically if it exits) …"
    supervise spoof "$SESSION/spoof.out" "$PY" "$SCRIPT_DIR/spoof_dsu.py"
    sleep 2   # wait for init messages + radar start-up
    if ! pgrep -f '^([^ ]*/)?(python[0-9.]* +([^ ]*/)?)?spoof_dsu\.py( |$)' >/dev/null; then
      echo "[roadlog] WARNING: spoof not running (radar without power? CAN down?) — keeps retrying. Last output:" >&2
      tail -n 3 "$SESSION/spoof.out" >&2
    fi
  fi
else
  echo "[roadlog] --no-spoof: assuming the spoof is running elsewhere."
fi

# ── CAN loggers (canonical log format, canplayer-compatible) ─────────────────
# candump's own error messages would land in the log; they go to candump.err.
echo "[roadlog] Logging $RADAR_IF → can_radar.log"
supervise candump-radar "$SESSION/can_radar.log" sh -c "exec candump -L $RADAR_IF 2>>'$SESSION/candump.err'"

echo "[roadlog] Logging $CAR_IF → can_car.log"
supervise candump-car "$SESSION/can_car.log" sh -c "exec candump -L $CAR_IF 2>>'$SESSION/candump.err'"

# ── GNSS logger (optional; ERB or NMEA, format detected from the data) ──────
if [[ "$GNSS" != "none" ]]; then
  echo "[roadlog] GNSS: $GNSS${GNSS_NOTE:+ ($GNSS_NOTE)} → gnss.csv, gnss.raw"
  "$PY" "$SCRIPT_DIR/gnsslog.py" --source "$GNSS" --out-dir "$SESSION" \
    > "$SESSION/gnss.out" 2>&1 &
  PIDS+=("$!")
  sleep 1
  if ! kill -0 "${PIDS[-1]}" 2>/dev/null; then
    echo "[roadlog] WARNING: gnsslog.py exited — recording without GNSS. Output:" >&2
    tail -n 3 "$SESSION/gnss.out" >&2
  fi
else
  echo "[roadlog] No GNSS (${GNSS_NOTE:---gnss none}) — CAN log only (fine for a baseline)."
fi

echo
echo "[roadlog] ▶ RECORDING.  Ctrl+C to stop."
echo "[roadlog]   (map afterwards:  tools/radarmap.py $SESSION/can_radar.log)"
echo

# ── Live status until Ctrl+C ─────────────────────────────────────────────────
# roadstatus.py only reads the growing log files. It runs in the background
# and we `wait` for it: unlike a foreground child, `wait` lets the INT/TERM
# trap run at once. If it fails (e.g. DBC missing), fall back to the plain
# heartbeat below — the recording goes on either way.
REC_START="$(date +%s.%N)"
"$PY" "$SCRIPT_DIR/roadstatus.py" "$SESSION" --start "$REC_START" \
  2> "$SESSION/roadstatus.err" &
PIDS+=("$!")
wait "$!"
echo "[roadlog] status display ended (see roadstatus.err) — plain heartbeat:" >&2
SECS=0
while true; do
  sleep 5
  SECS=$((SECS + 5))
  frames="$(wc -l < "$SESSION/can_radar.log" 2>/dev/null || echo 0)"
  printf "\r[roadlog] running %4ds  |  can_radar: %s frames " "$SECS" "$frames"
done
