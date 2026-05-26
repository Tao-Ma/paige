#!/usr/bin/env bash
# Host-side lifecycle for paige.
#
# This is the ONLY script allowed to touch ~/.paige/venv and the
# running bot. The dev container is for build-and-test; this script
# is for install-and-run.
#
# Two-environment flow:
#   1. (in dev container) ./do.sh artifact --export ~/.paige/wheels/
#   2. (on host)          ./scripts/prod.sh upgrade ~/.paige/wheels/paige-x.y.z.whl
#
# Commands:
#   status                        PID, installed wheel, uptime, log path
#   start                         nohup paige > $LOG; write PID
#   stop                          SIGTERM + 15s grace → SIGKILL
#   restart                       stop → start
#   logs [-f]                     tail $LOG
#   upgrade <wheel>               snapshot → stop → install → start
#                                 → health check → auto-rollback on failure
#
# Lifecycle details:
#   - PID file: ~/.paige/paige.pid
#   - Log:      ~/.paige/paige.log (persistent — survives reboots)
#   - Venv:     ~/.paige/venv
#   - Wheels:   ~/.paige/wheels/  (last 3 kept; older pruned on upgrade)
#   - Previous: ~/.paige/wheels/.previous/  (rollback snapshot)
#
# Does NOT skip hooks, does NOT use SIGKILL as a first resort, does
# NOT run multiple paige versions side-by-side. The host is
# single-tenant by design.

set -euo pipefail

PAIGE_DIR="${PAIGE_DIR:-$HOME/.paige}"
VENV="$PAIGE_DIR/venv"
PID_FILE="$PAIGE_DIR/paige.pid"
LOG_FILE="$PAIGE_DIR/paige.log"
WHEEL_DIR="$PAIGE_DIR/wheels"
PREVIOUS_DIR="$WHEEL_DIR/.previous"
HEALTH_TIMEOUT=30
STOP_GRACE=15
WHEEL_RETAIN=3
HEALTH_NEEDLE="App started"

usage() {
  cat <<'EOF'
Usage: ./scripts/prod.sh <command> [args]

Commands:
  status                  PID, installed wheel, uptime, log path
  start                   Launch paige; write PID
  stop                    SIGTERM + 15s grace → SIGKILL
  restart                 stop → start
  logs [-f]               tail the log file
  upgrade <wheel>         install wheel + restart with auto-rollback

Env:
  PAIGE_DIR (default ~/.paige)
EOF
}

# ── helpers ──────────────────────────────────────────────────────

mkdir -p "$PAIGE_DIR" "$WHEEL_DIR"

read_pid() {
  [ -f "$PID_FILE" ] || return 1
  local pid
  pid=$(cat "$PID_FILE" 2>/dev/null || true)
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && echo "$pid"
}

# Adopt a manually-launched paige (one started without writing the
# PID file). Looks for `paige` running out of OUR venv specifically
# so we don't accidentally adopt a different install.
adopt_pid() {
  local found
  found=$(pgrep -f "$VENV/bin/paige" 2>/dev/null | head -n 1)
  if [ -n "$found" ]; then
    echo "$found" > "$PID_FILE"
    echo "[paige] adopted running pid $found"
    echo "$found"
  fi
}

current_pid() {
  read_pid && return 0
  adopt_pid
}

current_wheel_version() {
  [ -x "$VENV/bin/pip" ] || { echo "(no install)"; return; }
  "$VENV/bin/pip" show paige 2>/dev/null \
    | awk '/^Version:/ {print $2; exit}' \
    || echo "(unknown)"
}

uptime_for() {
  local pid="$1"
  [ -r "/proc/$pid/stat" ] || { echo "?"; return; }
  local etime
  etime=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ' || true)
  echo "${etime:-?}"
}

prune_wheels() {
  local kept=0
  # Keep the most recent $WHEEL_RETAIN wheels by mtime; delete older.
  for w in $(ls -1t "$WHEEL_DIR"/paige-*.whl 2>/dev/null); do
    kept=$((kept + 1))
    if [ "$kept" -gt "$WHEEL_RETAIN" ]; then
      rm -f "$w"
      echo "[paige] pruned old wheel $(basename "$w")"
    fi
  done
}

ensure_venv() {
  if [ ! -x "$VENV/bin/python" ]; then
    echo "[paige] creating venv at $VENV"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
  fi
}

snapshot_current() {
  [ -x "$VENV/bin/pip" ] || return 0
  local current_wheel
  # Find the most recent wheel that matches the currently-installed version.
  local current_ver
  current_ver=$(current_wheel_version)
  current_wheel=$(ls -1t "$WHEEL_DIR"/paige-${current_ver}-*.whl 2>/dev/null | head -n 1 || true)
  [ -n "$current_wheel" ] || return 0
  rm -rf "$PREVIOUS_DIR"
  mkdir -p "$PREVIOUS_DIR"
  cp "$current_wheel" "$PREVIOUS_DIR/"
  echo "[paige] snapshot: $(basename "$current_wheel")"
}

install_wheel() {
  local wheel="$1"
  ensure_venv
  echo "[paige] installing $(basename "$wheel")"
  "$VENV/bin/pip" install --quiet --force-reinstall --no-deps "$wheel"
  # Pull in deps separately so we don't reinstall every transitive
  # package on each upgrade. Feishu + screenshot + voice are
  # included so the host venv covers the full optional surface.
  "$VENV/bin/pip" install --quiet "$wheel[feishu,screenshot,voice]"
}

start_bg() {
  ensure_venv
  if read_pid >/dev/null; then
    echo "[paige] already running (pid $(read_pid))"
    return 0
  fi
  # Load $PAIGE_DIR/.env into the environment of the launched bot.
  # paige's Config.from_env reads os.environ; without this, secrets
  # placed in ~/.paige/.env would never reach the process.
  if [ -f "$PAIGE_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1090,SC1091
    . "$PAIGE_DIR/.env"
    set +a
  fi
  # Strip outbound-proxy vars — Feishu's WS shouldn't go through
  # an HTTP/SOCKS5 proxy, and lark-oapi's WS doesn't speak SOCKS5
  # cleanly.
  unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY \
        all_proxy ALL_PROXY no_proxy NO_PROXY
  echo "[paige] starting → $LOG_FILE"
  nohup "$VENV/bin/paige" >> "$LOG_FILE" 2>&1 &
  local pid=$!
  disown 2>/dev/null || true
  echo "$pid" > "$PID_FILE"
  echo "[paige] pid $pid"
}

stop_running() {
  local pid
  pid=$(read_pid 2>/dev/null || true)
  if [ -z "$pid" ]; then
    echo "[paige] not running"
    rm -f "$PID_FILE"
    return 0
  fi
  echo "[paige] stopping pid $pid (SIGTERM, ${STOP_GRACE}s grace)"
  kill -TERM "$pid" 2>/dev/null || true
  local i=0
  while [ "$i" -lt "$STOP_GRACE" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "[paige] stopped cleanly"
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  echo "[paige] WARNING: ${STOP_GRACE}s grace expired; SIGKILL pid $pid" >&2
  kill -KILL "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
}

wait_for_healthy() {
  local pid="$1"
  local i=0
  while [ "$i" -lt "$HEALTH_TIMEOUT" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "[paige] FAIL: process exited during health check" >&2
      return 1
    fi
    if grep -q "$HEALTH_NEEDLE" "$LOG_FILE" 2>/dev/null; then
      echo "[paige] healthy"
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  echo "[paige] FAIL: '$HEALTH_NEEDLE' not seen in $HEALTH_TIMEOUT s" >&2
  return 1
}

rollback() {
  local prev_wheel
  prev_wheel=$(ls -1 "$PREVIOUS_DIR"/paige-*.whl 2>/dev/null | head -n 1 || true)
  if [ -z "$prev_wheel" ]; then
    echo "[paige] FAIL: no snapshot to roll back to" >&2
    return 1
  fi
  echo "[paige] rolling back → $(basename "$prev_wheel")"
  install_wheel "$prev_wheel"
  start_bg
}

# ── commands ─────────────────────────────────────────────────────

cmd_status() {
  local pid
  pid=$(current_pid 2>/dev/null || true)
  echo "wheel:   $(current_wheel_version)"
  echo "pid:     ${pid:-(not running)}"
  if [ -n "$pid" ]; then
    echo "uptime:  $(uptime_for "$pid")"
  fi
  echo "log:     $LOG_FILE"
  echo "venv:    $VENV"
  echo "wheels:  $WHEEL_DIR"
}

cmd_logs() {
  if [ ! -f "$LOG_FILE" ]; then
    echo "[paige] no log yet at $LOG_FILE"
    return 0
  fi
  if [ "${1:-}" = "-f" ]; then
    tail -f "$LOG_FILE"
  else
    tail -n 100 "$LOG_FILE"
  fi
}

cmd_upgrade() {
  local wheel="${1:-}"
  if [ -z "$wheel" ] || [ ! -f "$wheel" ]; then
    echo "[paige] usage: upgrade <wheel>" >&2
    exit 1
  fi
  # Copy into the wheel store so we have it for future rollback.
  cp -n "$wheel" "$WHEEL_DIR/" 2>/dev/null || true
  prune_wheels
  snapshot_current
  stop_running
  if ! install_wheel "$wheel"; then
    echo "[paige] FAIL: install failed; attempting rollback" >&2
    rollback || true
    exit 1
  fi
  start_bg
  local pid
  pid=$(read_pid 2>/dev/null || true)
  if [ -z "$pid" ]; then
    echo "[paige] FAIL: process not running after start" >&2
    rollback || true
    exit 1
  fi
  if ! wait_for_healthy "$pid"; then
    echo "[paige] FAIL: health check failed; rolling back" >&2
    stop_running
    rollback || true
    exit 1
  fi
  echo "[paige] upgrade ok"
}

cmd="${1:-help}"
shift || true
case "$cmd" in
  status)   cmd_status ;;
  start)    start_bg ;;
  stop)     stop_running ;;
  restart)  stop_running; start_bg ;;
  logs)     cmd_logs "$@" ;;
  upgrade)  cmd_upgrade "$@" ;;
  help|-h|--help) usage ;;
  *) echo "[paige] unknown command: $cmd" >&2; usage; exit 1 ;;
esac
