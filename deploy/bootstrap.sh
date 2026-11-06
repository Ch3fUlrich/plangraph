#!/usr/bin/env bash
set -euo pipefail

# Plangraph Spawner Host Bootstrap Script (Linux / macOS)

DRY_RUN=0
SKIP_AUTOOS=0
AUTOOS_ROOT=""
CATALOG_DB_PATH=""
PROBE_HOST=""
PROBE_CONFIG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --probe-host|--probe-config)
      [[ $# -ge 2 && -n "$2" ]] || { echo "Missing probe argument" >&2; exit 1; }
      if [[ "$1" == --probe-host ]]; then PROBE_HOST="$2"; else PROBE_CONFIG="$2"; fi
      shift 2 ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --skip-autoos)
      SKIP_AUTOOS=1
      shift
      ;;
    --autoos-root)
      AUTOOS_ROOT="$2"
      shift 2
      ;;
    --catalog-db-path)
      CATALOG_DB_PATH="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ_FILE="$SCRIPT_DIR/requirements-host.json"

if [[ ! -f "$REQ_FILE" ]]; then
  echo "Host requirements file not found: $REQ_FILE" >&2
  exit 1
fi

PYTHON_CMD="python3"
if ! command -v python3 &>/dev/null && command -v python &>/dev/null; then
  PYTHON_CMD="python"
fi

if [[ -n "$PROBE_HOST$PROBE_CONFIG" ]]; then
  "$PYTHON_CMD" -c '
import os, pathlib, re, shlex, stat, sys, tempfile
host, name, dry = sys.argv[1:]
valid = lambda h: re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", h) is not None
assert valid(host) and name, "paired probe host/config required; invalid host"
path = pathlib.Path(name)
mode = path.stat().st_mode
assert path.is_file() and not mode & 0o077, "config must exist and be private"
text = path.read_bytes().decode("utf-8")
lines = text.splitlines(keepends=True)
entries = [(i, line.split("=", 1)[1].strip()) for i, line in enumerate(lines)
           if re.match(r"^\s*(?:export\s+)?PLANGRAPH_PROBE_HOSTS\s*=", line)]
assert len(entries) == 1, "exactly one hosts assignment required"
index, value = entries[0]
hosts = shlex.split(value)
if len(hosts) == 1: hosts = hosts[0].split()
assert all(valid(h) for h in hosts), "invalid configured host"
if host in hosts: sys.exit(0)
if dry == "1": print("Would add probe host " + host); sys.exit(0)
lines[index] = "PLANGRAPH_PROBE_HOSTS=" + shlex.quote(" ".join(hosts + [host])) + "\n"
fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
try:
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), stat.S_IMODE(mode)); stream.write("".join(lines).encode("utf-8"))
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary): os.unlink(temporary)
' "$PROBE_HOST" "$PROBE_CONFIG" "$DRY_RUN"
  exit $?
fi

COMPONENTS="$("$PYTHON_CMD" -c '
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
c = data.get("autoos_components", {})
items = c.get("core", []) + c.get("agents", [])
print(",".join(items))
' "$REQ_FILE")"

echo "Resolved host components: $COMPONENTS"

"$PYTHON_CMD" -m plangraph.spawner.stall_watchdog tick --dry-run

if [[ "$SKIP_AUTOOS" -eq 0 && -n "$AUTOOS_ROOT" ]]; then
  if [[ -n "$AUTOOS_ROOT" && -f "$AUTOOS_ROOT/setup.sh" ]]; then
    SETUP_ARGS=("--only" "$COMPONENTS" "--yes")
    if [[ "$DRY_RUN" -eq 1 ]]; then
      SETUP_ARGS+=("--dry-run")
      echo "[DRY-RUN] Would execute: bash $AUTOOS_ROOT/setup.sh ${SETUP_ARGS[*]}"
    else
      bash "$AUTOOS_ROOT/setup.sh" "${SETUP_ARGS[@]}"
    fi
  else
    echo "AutoOS setup.sh not found (AUTOOS_ROOT='$AUTOOS_ROOT')." >&2
    exit 1
  fi
fi

echo "Initializing Plangraph spawner catalog database..."
INIT_ARGS=("-m" "plangraph.spawner.catalog_db" "init-and-seed")
if [[ -n "$CATALOG_DB_PATH" ]]; then
  INIT_ARGS+=("--db-path" "$CATALOG_DB_PATH")
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[DRY-RUN] Would execute: $PYTHON_CMD ${INIT_ARGS[*]}"
else
  "$PYTHON_CMD" "${INIT_ARGS[@]}"
fi

WATCHDOG_ARGS=("-m" "plangraph.spawner.stall_watchdog" "install" "--host" "linux")
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[DRY-RUN] Would execute: $PYTHON_CMD ${WATCHDOG_ARGS[*]}"
else
  "$PYTHON_CMD" "${WATCHDOG_ARGS[@]}"
fi

echo "Plangraph bootstrap completed successfully."
