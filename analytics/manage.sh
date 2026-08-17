#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$SCRIPT_DIR/.venv/bin/python"
RUNTIME_DIR="$SCRIPT_DIR/.runtime"
PID_FILE="$RUNTIME_DIR/aiflow-analytics.pid"
LOG_FILE="$RUNTIME_DIR/service.log"
ENV_FILE="$SCRIPT_DIR/.env"

runtime_value() {
  local key="$1"
  local fallback="$2"
  "$PYTHON" - "$key" "$fallback" "$ENV_FILE" <<'PY'
import os
import sys
from dotenv import dotenv_values

key, fallback, env_path = sys.argv[1:]
value = dotenv_values(env_path).get(key) if os.path.exists(env_path) else None
print(value or fallback)
PY
}

require_runtime() {
  if [[ ! -x "$PYTHON" ]]; then
    echo "Analytics environment missing. Run: ./manage.sh install" >&2
    exit 1
  fi
}

require_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "Missing $ENV_FILE. Create it from .env.example and configure credentials." >&2
    exit 1
  fi
}

read_pid() {
  [[ -f "$PID_FILE" ]] && tr -d '[:space:]' < "$PID_FILE"
}

owns_process() {
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  local command
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ "$command" == *"server.py"* ]] || return 1
  if command -v lsof >/dev/null 2>&1; then
    local cwd
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
    [[ -z "$cwd" || "$cwd" == "$SCRIPT_DIR" ]] || return 1
  fi
}

health_url() {
  local port
  port="$(runtime_value AIFLOW_ANALYTICS_PORT 5090)"
  echo "http://127.0.0.1:${port}/ready"
}

wait_ready() {
  local pid="$1"
  local url
  url="$(health_url)"
  for _ in {1..60}; do
    if curl --silent --fail --max-time 2 "$url" >/dev/null 2>&1; then
      echo "Analytics ready: $url"
      return 0
    fi
    if ! owns_process "$pid"; then
      echo "Analytics exited before readiness. Last log lines:" >&2
      tail -n 40 "$LOG_FILE" 2>/dev/null || true
      return 1
    fi
    sleep 1
  done
  echo "Analytics readiness timed out: $url" >&2
  return 1
}

install_service() {
  local bootstrap_python="${AIFLOW_ANALYTICS_PYTHON:-python3}"
  if ! "$bootstrap_python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "AIFlow Analytics requires Python 3.11 or newer. Set AIFLOW_ANALYTICS_PYTHON to a compatible interpreter." >&2
    exit 1
  fi
  "$bootstrap_python" -m venv "$SCRIPT_DIR/.venv"
  "$SCRIPT_DIR/.venv/bin/pip" install --upgrade pip
  "$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"
  echo "Analytics dependencies installed."
}

run_service() {
  require_runtime
  require_env
  cd "$SCRIPT_DIR"
  exec "$PYTHON" server.py
}

start_service() {
  require_runtime
  require_env
  mkdir -p "$RUNTIME_DIR"
  local existing
  existing="$(read_pid || true)"
  if [[ -n "$existing" ]] && owns_process "$existing"; then
    echo "Analytics already running (PID $existing)."
    return 0
  fi
  rm -f "$PID_FILE"
  (
    cd "$SCRIPT_DIR"
    nohup "$PYTHON" server.py >> "$LOG_FILE" 2>&1 &
    echo "$!" > "$PID_FILE"
  )
  local pid
  pid="$(read_pid)"
  echo "Starting AIFlow Analytics (PID $pid)..."
  wait_ready "$pid"
}

stop_service() {
  local pid
  pid="$(read_pid || true)"
  if [[ -z "$pid" ]]; then
    echo "Analytics is not running."
    return 0
  fi
  if ! owns_process "$pid"; then
    echo "PID file does not belong to this analytics directory; refusing PID $pid." >&2
    rm -f "$PID_FILE"
    return 1
  fi
  kill -TERM "$pid"
  for _ in {1..15}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "Analytics stopped."
      return 0
    fi
    sleep 1
  done
  if owns_process "$pid"; then
    kill -KILL "$pid"
  fi
  rm -f "$PID_FILE"
  echo "Analytics force-stopped after timeout."
}

status_service() {
  require_runtime
  local pid
  pid="$(read_pid || true)"
  if [[ -z "$pid" ]] || ! owns_process "$pid"; then
    echo "Analytics: stopped"
    return 1
  fi
  local url
  url="$(health_url)"
  if curl --silent --fail --max-time 2 "$url" >/dev/null 2>&1; then
    echo "Analytics: running (PID $pid, ready, $url)"
    return 0
  fi
  echo "Analytics: running (PID $pid), readiness failed: $url"
  return 1
}

show_logs() {
  mkdir -p "$RUNTIME_DIR"
  touch "$LOG_FILE"
  tail -n 100 -f "$LOG_FILE"
}

show_config() {
  require_runtime
  require_env
  cd "$SCRIPT_DIR"
  "$PYTHON" - <<'PY'
import json
from aiflow_analytics.config import Settings, settings_summary

print(json.dumps(settings_summary(Settings.from_env()), ensure_ascii=False, indent=2))
PY
}

show_help() {
  cat <<'EOF'
Usage: ./manage.sh <command>

Commands:
  install   Create .venv and install runtime dependencies
  run       Run analytics in the foreground
  start     Start analytics and wait for /ready
  resume    Alias for start
  stop      Stop only this analytics directory's recorded process
  pause     Alias for stop
  restart   Stop, start, and verify readiness
  status    Check PID ownership and /ready
  logs      Follow the analytics service log
  config    Print effective non-secret configuration
  help      Show this help
EOF
}

command="${1:-help}"
case "$command" in
  install) install_service ;;
  run) run_service ;;
  start|resume) start_service ;;
  stop|pause) stop_service ;;
  restart) stop_service; start_service ;;
  status) status_service ;;
  logs) show_logs ;;
  config) show_config ;;
  help|-h|--help) show_help ;;
  *) echo "Unknown command: $command" >&2; show_help; exit 2 ;;
esac
