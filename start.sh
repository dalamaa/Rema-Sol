#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$ROOT_DIR/.remasol.pid"
LOG_FILE="$ROOT_DIR/remasol.log"
LOCK_DIR="$ROOT_DIR/.remasol.start.lock"

cleanup_lock() {
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup_lock EXIT

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "RemaSol start is already in progress." >&2
  exit 1
fi

choose_python() {
  if [[ -n "${REMA_SOL_PYTHON:-}" && -x "$REMA_SOL_PYTHON" ]]; then
    printf '%s\n' "$REMA_SOL_PYTHON"
  elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
    printf '%s\n' "$ROOT_DIR/.venv/bin/python"
  elif [[ -x "$ROOT_DIR/venv/bin/python" ]]; then
    printf '%s\n' "$ROOT_DIR/venv/bin/python"
  elif [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then
    printf '%s\n' "$VIRTUAL_ENV/bin/python"
  else
    command -v python3
  fi
}

process_is_remasol() {
  local pid="$1"
  local command executable cwd
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  executable="$(ps -p "$pid" -o comm= 2>/dev/null || true)"
  case "${executable##*/}" in
    python|python3|python3.*) ;;
    *) return 1 ;;
  esac
  if [[ "$command" == *"$ROOT_DIR/app.py"* ]]; then
    return 0
  fi
  if [[ "$command" == "app.py" || "$command" == *" app.py"* ]]; then
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
    [[ "$cwd" == "$ROOT_DIR" ]]
    return
  fi
  return 1
}

process_is_listening() {
  local pid="$1"
  lsof -a -p "$pid" -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
}

find_remasol_pid() {
  while read -r pid _command; do
    [[ "$pid" =~ ^[0-9]+$ && "$pid" != "$$" ]] || continue
    if process_is_remasol "$pid"; then
      printf '%s\n' "$pid"
      return 0
    fi
  done < <(ps -axo pid=,command= 2>/dev/null)
  return 1
}

if [[ -f "$PID_FILE" ]]; then
  existing_pid="$(<"$PID_FILE")"
  if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
    if process_is_remasol "$existing_pid"; then
      echo "RemaSol is already running (PID $existing_pid)."
      exit 0
    fi
    echo "Refusing to use $PID_FILE: PID $existing_pid is not a RemaSol process." >&2
    exit 1
  fi
  rm -f -- "$PID_FILE"
fi

discovered_pid="$(find_remasol_pid || true)"
if [[ "$discovered_pid" =~ ^[0-9]+$ ]] && process_is_remasol "$discovered_pid"; then
  printf '%s\n' "$discovered_pid" >"$PID_FILE"
  echo "RemaSol is already running (PID $discovered_pid)."
  exit 0
fi

PYTHON_BIN="$(choose_python)"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "No usable Python interpreter found. Create .venv or set REMA_SOL_PYTHON." >&2
  exit 1
fi

cd "$ROOT_DIR"
# app.py calls load_dotenv(), so the existing .env is loaded from this stable
# project directory. The local launcher owns the documented Flask port.
export PORT=5007

"$PYTHON_BIN" "$ROOT_DIR/app.py" >"$LOG_FILE" 2>&1 &
server_pid=$!
printf '%s\n' "$server_pid" >"$PID_FILE"

for _ in {1..20}; do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    rm -f -- "$PID_FILE"
    echo "RemaSol failed to start. See $LOG_FILE." >&2
    tail -n 30 "$LOG_FILE" >&2 || true
    exit 1
  fi
  if process_is_remasol "$server_pid" && process_is_listening "$server_pid"; then
    echo "RemaSol started on http://127.0.0.1:${PORT} (PID $server_pid)."
    exit 0
  fi
  sleep 0.1
done

rm -f -- "$PID_FILE"
echo "RemaSol failed to open port ${PORT}. See $LOG_FILE." >&2
tail -n 30 "$LOG_FILE" >&2 || true
exit 1
