#!/usr/bin/env bash
set -euo pipefail

# Plangraph Spawner Host Bootstrap Script (Linux / macOS)

DRY_RUN=0
SKIP_AUTOOS=0
AUTOOS_ROOT="${AUTOOS_ROOT:-}"
CATALOG_DB_PATH=""

while [[ $# -gt 0 ]]; do
  case "$1" in
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

COMPONENTS="$("$PYTHON_CMD" -c '
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as f:
    data = json.load(f)
c = data.get("autoos_components", {})
items = c.get("core", []) + c.get("agents", [])
print(",".join(items))
' "$REQ_FILE")"

echo "Resolved host components: $COMPONENTS"

if [[ "$SKIP_AUTOOS" -eq 0 ]]; then
  if [[ -z "$AUTOOS_ROOT" ]]; then
    for cand in "$SCRIPT_DIR/../../AutoOS" "$SCRIPT_DIR/../../../AutoOS" "$HOME/Documents/Code/AutoOS" "$HOME/.cache/plangraph/AutoOS"; do
      if [[ -f "$cand/setup.sh" ]]; then
        AUTOOS_ROOT="$(cd "$cand" && pwd)"
        break
      fi
    done
  fi

  if [[ -n "$AUTOOS_ROOT" && -f "$AUTOOS_ROOT/setup.sh" ]]; then
    SETUP_ARGS=("--only" "$COMPONENTS" "--yes")
    if [[ "$DRY_RUN" -eq 1 ]]; then
      SETUP_ARGS+=("--dry-run")
      echo "[DRY-RUN] Would execute: bash $AUTOOS_ROOT/setup.sh ${SETUP_ARGS[*]}"
    else
      bash "$AUTOOS_ROOT/setup.sh" "${SETUP_ARGS[@]}"
    fi
  else
    echo "Warning: AutoOS setup.sh not found (AUTOOS_ROOT='$AUTOOS_ROOT'). Skipping AutoOS invocation." >&2
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

echo "Plangraph bootstrap completed successfully."
