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
  "$PYTHON_CMD" -m plangraph.spawner.probe_enroll "$PROBE_HOST" "$PROBE_CONFIG" "$DRY_RUN"
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

# Omnigraph: site config, cache client, session environment, health verification.
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Omnigraph dry-run: read site config; cache client; install session environment; verify health"
else
  "$PYTHON_CMD" "$SCRIPT_DIR/omnigraph_step.py" --dry-run "$DRY_RUN" || echo "Omnigraph skipped: helper unavailable"
fi

echo "Plangraph bootstrap completed successfully."
