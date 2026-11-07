#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$ROOT_DIR/.remasol.pid"

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
  pid="$(<"$PID_FILE")"
else
  pid="$(find_remasol_pid || true)"
  if [[ -z "$pid" ]]; then
    echo "RemaSol is not running (no tracked PID file or matching app process)."
    exit 0
  fi
fi

if [[ ! "$pid" =~ ^[0-9]+$ ]]; then
  echo "Refusing to stop an invalid RemaSol PID file." >&2
  exit 1
fi

if ! kill -0 "$pid" 2>/dev/null; then
  rm -f -- "$PID_FILE"
  echo "Removed a stale RemaSol PID file; no server was running."
  exit 0
fi

if ! process_is_remasol "$pid"; then
  echo "Refusing to stop PID $pid: it is not the RemaSol app at $ROOT_DIR/app.py." >&2
  exit 1
fi

kill -TERM "$pid"
for _ in {1..100}; do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f -- "$PID_FILE"
    echo "RemaSol stopped gracefully (PID $pid)."
    exit 0
  fi
  sleep 0.1
done

echo "RemaSol did not exit after the graceful stop request; PID $pid was left untouched." >&2
exit 1
