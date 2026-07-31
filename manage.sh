#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python"
RUNTIME_DIR="$ROOT_DIR/.runtime"
PID_FILE="$RUNTIME_DIR/server.pid"
LOG_FILE="$RUNTIME_DIR/server.log"
ENV_FILE="${AIFLOW_ENV_FILE:-$ROOT_DIR/.env.local}"

load_local_env() {
  if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
  fi
}

load_local_env

mkdir -p "$RUNTIME_DIR"

usage() {
  cat <<'EOF'
Usage: ./manage.sh <command>

Commands:
  install   Create .venv and install runtime dependencies
  run       Run the service in the foreground
  start     Start the service in the background and verify /health
  stop      Stop the managed background service
  restart   Restart the managed background service
  status    Show process state and perform a live health check
  logs      Follow the managed service log
  config    Show the effective model provider configuration (secrets masked)
  client    Open or print the local web client URL
  open      Open the API documentation in the default browser
  test      Install dev dependencies and run the test suite
  help      Show this help
EOF
}

runtime_python_works() {
  [[ -x "$PYTHON" ]] && /bin/sh -c '"$1" -c "import sys" >/dev/null 2>&1' sh "$PYTHON" >/dev/null 2>&1
}

require_runtime() {
  if ! runtime_python_works; then
    echo "Runtime is not installed. Run: ./manage.sh install" >&2
    exit 1
  fi
  if ! "$PYTHON" -c 'import fastapi, claude_agent_sdk, uvicorn' >/dev/null 2>&1; then
    echo "Runtime dependencies are incomplete. Run: ./manage.sh install" >&2
    exit 1
  fi
}

read_endpoint() {
  require_runtime
  "$PYTHON" - <<'PY'
from aiflow_server.config import load_settings
s = load_settings()
print(s.host)
print(s.port)
PY
}

primary_lan_ip() {
  local interface_name lan_ip
  lan_ip=""
  if command -v ip >/dev/null 2>&1; then
    lan_ip="$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}' || true)"
  elif command -v route >/dev/null 2>&1 && command -v ipconfig >/dev/null 2>&1; then
    interface_name="$(route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}' || true)"
    if [[ -n "$interface_name" ]]; then
      lan_ip="$(ipconfig getifaddr "$interface_name" 2>/dev/null || true)"
    fi
  elif command -v hostname >/dev/null 2>&1; then
    lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  printf '%s\n' "$lan_ip"
}

show_access_urls() {
  local host="$1" port="$2" lan_ip
  if [[ "$host" == "0.0.0.0" ]]; then
    echo "Local client: http://127.0.0.1:$port/client"
    lan_ip="$(primary_lan_ip)"
    if [[ -n "$lan_ip" ]]; then
      echo "LAN client:   http://$lan_ip:$port/client"
    else
      echo "LAN client:   http://<server-lan-ip>:$port/client"
    fi
  else
    echo "Client:       http://$host:$port/client"
  fi
}

open_url() {
  local url="$1"
  if command -v open >/dev/null 2>&1; then
    open "$url"
  elif command -v xdg-open >/dev/null 2>&1 && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    xdg-open "$url"
  else
    echo "$url"
  fi
}

managed_pid() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(tr -d '[:space:]' < "$PID_FILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  local command
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ "$command" == *"uvicorn"* && "$command" == *"aiflow_server.gateway:app"* ]] || return 2
  printf '%s\n' "$pid"
}

install_runtime() {
  local bootstrap_python bootstrap_path backup_path failed_path stamp
  bootstrap_python="${AIFLOW_BOOTSTRAP_PYTHON:-python3}"
  if [[ "$bootstrap_python" == */* ]]; then
    bootstrap_path="$bootstrap_python"
  else
    bootstrap_path="$(command -v "$bootstrap_python" 2>/dev/null || true)"
  fi
  if [[ -z "$bootstrap_path" || ! -x "$bootstrap_path" ]]; then
    echo "Bootstrap Python is unavailable: $bootstrap_python" >&2
    echo "Install Python 3 or set AIFLOW_BOOTSTRAP_PYTHON to an executable Python path." >&2
    return 1
  fi

  backup_path=""
  if [[ -e "$VENV_DIR" ]] && ! runtime_python_works; then
    stamp="$(date '+%Y%m%d-%H%M%S')-$$"
    backup_path="$RUNTIME_DIR/venv-backups/venv-incompatible-$stamp"
    mkdir -p "$(dirname "$backup_path")"
    mv "$VENV_DIR" "$backup_path"
    echo "Existing incompatible virtual environment moved to: $backup_path"
  fi

  if ! runtime_python_works; then
    if ! "$bootstrap_path" -m venv "$VENV_DIR"; then
      echo "Failed to create the Python virtual environment." >&2
      echo "On Debian/Ubuntu, install the matching package first: sudo apt-get install python3-venv" >&2
      if [[ -n "$backup_path" && -e "$backup_path" ]]; then
        if [[ -e "$VENV_DIR" ]]; then
          failed_path="$RUNTIME_DIR/venv-backups/venv-failed-$stamp"
          mv "$VENV_DIR" "$failed_path"
          echo "Partial new environment moved to: $failed_path" >&2
        fi
        mv "$backup_path" "$VENV_DIR"
        echo "Previous virtual environment restored to: $VENV_DIR" >&2
      fi
      return 1
    fi
  fi
  "$PYTHON" -m pip install -r "$ROOT_DIR/requirements.txt"
}

run_foreground() {
  require_runtime
  local endpoint host port existing
  if existing="$(managed_pid)"; then
    echo "Service is already running (pid=$existing)" >&2
    return 1
  fi
  rm -f "$PID_FILE"
  endpoint="$(read_endpoint)"
  host="$(printf '%s\n' "$endpoint" | sed -n '1p')"
  port="$(printf '%s\n' "$endpoint" | sed -n '2p')"
  cd "$ROOT_DIR"
  printf '%s\n' "$$" > "$PID_FILE"
  exec "$PYTHON" -m uvicorn aiflow_server.gateway:app --host "$host" --port "$port"
}

start_background() {
  require_runtime
  local existing startup_timeout deadline
  if existing="$(managed_pid)"; then
    echo "Service is already running (pid=$existing)"
    return 0
  fi
  if [[ -f "$PID_FILE" ]]; then
    rm -f "$PID_FILE"
  fi

  local endpoint host port health_host
  endpoint="$(read_endpoint)"
  host="$(printf '%s\n' "$endpoint" | sed -n '1p')"
  port="$(printf '%s\n' "$endpoint" | sed -n '2p')"
  health_host="$host"
  [[ "$health_host" == "0.0.0.0" ]] && health_host="127.0.0.1"
  startup_timeout="${AIFLOW_START_TIMEOUT_SECONDS:-60}"
  if ! [[ "$startup_timeout" =~ ^[0-9]+$ ]] || (( startup_timeout < 1 )); then
    echo "AIFLOW_START_TIMEOUT_SECONDS must be a positive integer." >&2
    return 1
  fi

  cd "$ROOT_DIR"
  nohup "$PYTHON" -m uvicorn aiflow_server.gateway:app --host "$host" --port "$port" >>"$LOG_FILE" 2>&1 &
  local pid=$!
  printf '%s\n' "$pid" > "$PID_FILE"

  deadline=$((SECONDS + startup_timeout))
  while (( SECONDS < deadline )); do
    if curl -fsS --max-time 2 "http://$health_host:$port/health" >/dev/null 2>&1; then
      echo "Service started: http://$health_host:$port (pid=$pid)"
      show_access_urls "$host" "$port"
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.2
  done

  echo "Service failed to become healthy within ${startup_timeout}s. Recent log:" >&2
  tail -n 40 "$LOG_FILE" >&2 || true
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  return 1
}

stop_background() {
  local pid status_code
  set +e
  pid="$(managed_pid)"
  status_code=$?
  set -e
  if [[ $status_code -eq 2 ]]; then
    echo "PID file points to a different process; refusing to stop it." >&2
    exit 1
  fi
  if [[ $status_code -ne 0 ]]; then
    rm -f "$PID_FILE"
    echo "Service is not running"
    return 0
  fi

  kill "$pid"
  local attempt
  for attempt in {1..50}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$PID_FILE"
      echo "Service stopped"
      return 0
    fi
    sleep 0.2
  done
  echo "Service did not stop after 10 seconds; PID $pid was left untouched." >&2
  exit 1
}

show_status() {
  require_runtime
  local pid status_code endpoint host port health_host
  set +e
  pid="$(managed_pid)"
  status_code=$?
  set -e
  if [[ $status_code -eq 2 ]]; then
    echo "State: unsafe PID file (points to another process)"
    return 1
  fi
  if [[ $status_code -ne 0 ]]; then
    echo "State: stopped"
    return 1
  fi

  endpoint="$(read_endpoint)"
  host="$(printf '%s\n' "$endpoint" | sed -n '1p')"
  port="$(printf '%s\n' "$endpoint" | sed -n '2p')"
  health_host="$host"
  [[ "$health_host" == "0.0.0.0" ]] && health_host="127.0.0.1"
  echo "State: running (pid=$pid)"
  curl -fsS --max-time 3 "http://$health_host:$port/health"
  echo
  curl -fsS --max-time 3 "http://$health_host:$port/ready"
  echo
  show_access_urls "$host" "$port"
}

show_config() {
  require_runtime
  "$PYTHON" - <<'PY'
import os

from aiflow_server.config import load_settings

settings = load_settings()
base_url = os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
auth_sources = [
    name
    for name in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
    if os.environ.get(name)
]
print(f"Listen: {settings.host}:{settings.port}")
print(f"Model ID: {settings.claude_model or 'claude-code-default'}")
print(f"Provider URL: {base_url}")
print(f"Authentication: {', '.join(auth_sources) if auth_sources else 'not configured'}")
print("Public entrypoint: anonymous web BFF (aiflow_server.gateway)")
print("Private core authentication: enabled with an in-memory BFF key")
print("Browser-held signing secret: none")
print(
    "AI task limits: "
    f"{settings.max_ai_tasks_per_client_minute}/client/minute, "
    f"{settings.max_ai_tasks_per_client_day}/client/day, "
    f"{settings.max_ai_tasks_global_day}/global/day"
)
print(
    "Anonymous web limits: "
    f"{settings.web_ai_tasks_per_session_minute}/session/minute, "
    f"{settings.web_ai_tasks_per_session_day}/session/day, "
    f"{settings.web_ai_tasks_per_ip_day}/IP/day"
)
print(f"Environment file: {os.environ.get('AIFLOW_ENV_FILE') or '.env.local (when present)'}")
if len(auth_sources) > 1:
    print("Warning: both authentication variables are set; keep only the one required by the provider.")
if not settings.web_cookie_secure:
    print("Warning: web_gateway.cookie_secure is false; set it to true after HTTPS is configured.")
PY
}

open_client() {
  local endpoint host port
  endpoint="$(read_endpoint)"
  host="$(printf '%s\n' "$endpoint" | sed -n '1p')"
  port="$(printf '%s\n' "$endpoint" | sed -n '2p')"
  [[ "$host" == "0.0.0.0" ]] && host="127.0.0.1"
  open_url "http://$host:$port/client"
}

open_docs() {
  local endpoint host port
  endpoint="$(read_endpoint)"
  host="$(printf '%s\n' "$endpoint" | sed -n '1p')"
  port="$(printf '%s\n' "$endpoint" | sed -n '2p')"
  [[ "$host" == "0.0.0.0" ]] && host="127.0.0.1"
  open_url "http://$host:$port/docs"
}

run_tests() {
  require_runtime
  "$PYTHON" -m pip install -r "$ROOT_DIR/requirements-dev.txt"
  cd "$ROOT_DIR"
  exec "$PYTHON" -m pytest -q
}

case "${1:-help}" in
  install) install_runtime ;;
  run) run_foreground ;;
  start) start_background ;;
  stop) stop_background ;;
  restart) stop_background; start_background ;;
  status) show_status ;;
  logs) touch "$LOG_FILE"; tail -f "$LOG_FILE" ;;
  config) show_config ;;
  client) open_client ;;
  open) open_docs ;;
  test) run_tests ;;
  help|-h|--help) usage ;;
  *) usage >&2; exit 2 ;;
esac
