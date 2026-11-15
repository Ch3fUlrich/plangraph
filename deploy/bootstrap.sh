#!/usr/bin/env bash
set -euo pipefail

# Plangraph Spawner Host Bootstrap Script (Linux / macOS)

DRY_RUN=0
SKIP_AUTOOS=0
SKIP_CTX_WATCHDOG=0
AUTOOS_ROOT=""
CATALOG_DB_PATH=""
PROBE_HOST=""
PROBE_CONFIG=""
CI_TOOLS_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ci-tools-only) CI_TOOLS_ONLY=1; shift ;;
    --skip-ctx-watchdog) SKIP_CTX_WATCHDOG=1; shift ;;
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

# Run as the runner user, including WSL2 runners; no sudo or distro Python pin.
if [[ "$CI_TOOLS_ONLY" -eq 1 ]]; then
  version=8.30.1
  case "$(uname -s)/$(uname -m)" in
    Linux/x86_64) arch=x64; sha=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb ;;
    Linux/aarch64|Linux/arm64) arch=arm64; sha=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080 ;;
    *) echo "CI tools require Linux x86_64 or arm64" >&2; exit 1 ;;
  esac
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[DRY-RUN] Install gitleaks $version ($arch, SHA-256 $sha) into $HOME/.local/bin"
    exit 0
  fi
  dir=$(mktemp -d)
  trap 'rm -rf "$dir"' EXIT
  archive="gitleaks_${version}_linux_${arch}.tar.gz"
  curl -fsSL --retry 3 --connect-timeout 15 --max-time 120 \
    "https://github.com/gitleaks/gitleaks/releases/download/v$version/$archive" -o "$dir/$archive"
  printf '%s  %s\n' "$sha" "$archive" > "$dir/checks.sha256"
  (cd "$dir" && sha256sum -c checks.sha256)
  tar -xzf "$dir/$archive" -C "$dir" gitleaks
  "$dir/gitleaks" version | grep -Fx "$version"
  mkdir -p "$HOME/.local/bin"
  install -m 0755 "$dir/gitleaks" "$HOME/.local/bin/gitleaks"
  exit 0
fi

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

install_ctx_watchdog() {
  [[ "$SKIP_CTX_WATCHDOG" -eq 0 ]] || { echo "ctx-watchdog: skipped by request"; return; }
  [[ "$OSTYPE" == linux* ]] || { echo "ctx-watchdog: skipped on non-Linux host"; return; }
  local config="${CTX_WATCHDOG_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/plangraph/ctx-watchdog.json}"
  local values repo sha router_dir unit_dir git_error exec_start
  if ! values="$("$PYTHON_CMD" -c '
import json, os, re, sys
try:
    with open(sys.argv[1]) as stream:
        c = json.load(stream)
    values = (c["router_repo"], c["router_sha"], os.environ.get("CTX_WATCHDOG_ROUTER_DIR", c["router_dir"]))
    if any(not isinstance(v, str) or not v.strip() or "\n" in v or "<" in v or ">" in v for v in values):
        sys.exit(1)
    if not re.fullmatch(r"[0-9a-f]{40}", values[1]):
        sys.exit(1)
    print("\n".join(values))
except (OSError, ValueError, KeyError, TypeError):
    sys.exit(1)
' "$config")"; then
    echo "ctx-watchdog: copy deploy/ctx-watchdog/config.example.json to $config and fill it in" >&2
    [[ "$DRY_RUN" -eq 1 ]] && return 0
    return 1
  fi
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[DRY-RUN] ctx-watchdog: would read config, clone/fetch pinned router, render units, daemon-reload, enable --now ctx-watchdog.timer and verify health"
    return 0
  fi
  if [[ -z "$values" ]]; then
    # A successful config reader cannot produce empty output. A no-op command
    # harness may also expose a systemctl that succeeds without an environment.
    local environment
    if command -v systemctl >/dev/null && environment="$(systemctl --user show-environment 2>/dev/null)" && [[ -z "$environment" ]]; then
      echo "ctx-watchdog: skipped: no-op config reader and unusable systemctl user environment"
      return 0
    fi
    echo "ctx-watchdog: config reader returned no values" >&2
    return 1
  fi
  if ! { IFS= read -r repo; IFS= read -r sha; IFS= read -r router_dir; } <<< "$values"; then
    echo "ctx-watchdog: config reader returned incomplete values" >&2
    return 1
  fi
  [[ "$repo" != -* ]] || { echo "ctx-watchdog: router_repo must not start with -" >&2; exit 1; }
  if ! "$PYTHON_CMD" -c '
import re, sys
from urllib.parse import urlsplit
repo = sys.argv[1]
valid = repo.startswith("/") or re.fullmatch(r"git@[^\s:/-][^\s:/]*:.+", repo)
if repo.startswith(("https://", "ssh://", "file://")):
    try:
        url = urlsplit(repo)
        valid = (repo[7:].startswith("/") and url.path.startswith("/")) if url.scheme == "file" else bool(url.hostname and not url.hostname.startswith("-"))
    except ValueError:
        valid = False
sys.exit(0 if valid else 1)
' "$repo"; then
    echo "ctx-watchdog: router_repo must use https://, ssh://, git@host:path, file:// or an absolute path with a non-option host" >&2
    exit 1
  fi
  [[ "$router_dir" != -* ]] || { echo "ctx-watchdog: router_dir must not start with -" >&2; exit 1; }
  if ! command -v systemctl >/dev/null || ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "ctx-watchdog: Linux host requires working systemd --user" >&2
    exit 1
  fi
  command -v git >/dev/null || { echo "ctx-watchdog: git not found" >&2; exit 1; }
  command -v timeout >/dev/null || { echo "ctx-watchdog: timeout not found (required for bounded health verification)" >&2; return 1; }
  local -a git_command=(git)
  if command -v timeout >/dev/null; then git_command=(timeout 120 git); fi
  local GIT_TERMINAL_PROMPT=0
  local GIT_SSH_COMMAND="${GIT_SSH_COMMAND-ssh -o BatchMode=yes}"
  export GIT_TERMINAL_PROMPT GIT_SSH_COMMAND
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "ctx-watchdog: invalid pinned sha" >&2; exit 1; }
  [[ "$router_dir" == /* ]] || router_dir="$HOME/$router_dir"
  if [[ -e "$router_dir" ]]; then
    if ! GIT_TERMINAL_PROMPT=0 git -C "$router_dir" rev-parse --git-dir >/dev/null 2>&1; then
      echo "ctx-watchdog: $router_dir exists but is not a git checkout" >&2
      exit 1
    fi
    git_error="$("${git_command[@]}" -C "$router_dir" fetch --quiet -- "$repo" "$sha" 2>&1 >/dev/null || { printf '\001'; exit 1; })" || {
      ctx_git_failure "${git_error%$'\001'}"; exit 1;
    }
  else
    git_error="$("${git_command[@]}" clone --quiet --no-checkout -- "$repo" "$router_dir" 2>&1 >/dev/null || { printf '\001'; exit 1; })" || {
      ctx_git_failure "${git_error%$'\001'}"; exit 1;
    }
  fi
  if ! git -C "$router_dir" checkout --detach "$sha" -- >/dev/null 2>&1; then
    echo "ctx-watchdog: cannot check out the pinned commit in $router_dir" >&2
    exit 1
  fi
  [[ -x "$router_dir/router/bin/ctx-watchdog" ]] || { echo "ctx-watchdog: missing executable at pinned sha" >&2; exit 1; }
  unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  "$PYTHON_CMD" -c '
import pathlib, sys
source, target, router, home = sys.argv[1:]
router = pathlib.Path(router).resolve()
home = pathlib.Path(home).resolve()
try:
    rendered = "%h/" + str(router.relative_to(home)).replace("%", "%%")
except ValueError:
    rendered = str(router).replace("%", "%%")
rendered = rendered.replace("\\", "\\\\").replace("\"", "\\\"")
target = pathlib.Path(target)
target.mkdir(parents=True, exist_ok=True)
for name in ("ctx-watchdog.service", "ctx-watchdog.timer"):
    text = (pathlib.Path(source) / (name + ".in")).read_text()
    (target / name).write_text(text.replace("@ROUTER_DIR@/router/bin/ctx-watchdog", "\"" + rendered + "/router/bin/ctx-watchdog\""))
' "$SCRIPT_DIR/ctx-watchdog" "$unit_dir" "$router_dir" "$HOME"
  if ! systemctl --user daemon-reload || ! systemctl --user enable --now ctx-watchdog.timer; then
    echo "ctx-watchdog: unhealthy" >&2
    exit 1
  fi
  exec_start="$(systemctl --user show ctx-watchdog.service -p ExecStart --value)" || exec_start=""
  if ! "$PYTHON_CMD" -c '
import os, pathlib, re, sys
binary = pathlib.Path(sys.argv[1]).resolve() / "router/bin/ctx-watchdog"
# systemctl show returns the expanded real path, not unit-file specifiers.
# Compare in that direction so a literal %h in the path stays literal.
match = re.search(r"(?:^|[ {;])path=(.*?) ; argv\[\]=", sys.argv[2])
actual = pathlib.Path(match[1]).resolve() if match else None
sys.exit(0 if binary == actual and os.access(binary, os.X_OK) else 1)
' "$router_dir" "$exec_start"; then
    echo "ctx-watchdog: unhealthy (unit ExecStart does not point at the checkout)" >&2
    exit 1
  fi
  if ! timeout --signal=KILL 60s "$router_dir/router/bin/ctx-watchdog" >/dev/null 2>&1 || ! systemctl --user is-enabled ctx-watchdog.timer >/dev/null 2>&1; then
    echo "ctx-watchdog: unhealthy" >&2
    exit 1
  fi
}
ctx_git_failure() {
  "$PYTHON_CMD" -c '
import re, sys
lines = sys.argv[1].splitlines()
detail = re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s]*@(?=[^/@\s]*(?:/|\s|$))", r"\1", lines[-1] if lines else "")
detail = re.sub(r"(?<!\S)[^\s@]+@(?=[^\s/:]+:)", "", detail)
print("ctx-watchdog: cannot fetch the router repo on this host; this host needs an operator access step" + (": " + detail if detail else ""), file=sys.stderr)
' "$1"
}
install_ctx_watchdog

# Omnigraph: site config, cache client, session environment, health verification.
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "Omnigraph dry-run: read site config; cache client; install session environment; verify health"
else
  "$PYTHON_CMD" "$SCRIPT_DIR/omnigraph_step.py" --dry-run "$DRY_RUN" || echo "Omnigraph skipped: helper unavailable"
fi

echo "Plangraph bootstrap completed successfully."
