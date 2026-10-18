#!/usr/bin/env bash
# devstack.sh — loopback-only dev stack for plangraph: `temporal server start-dev`, Postgres 16
# (docker, or a local initdb'd cluster under the state dir), and the keeper daemon.
# Deliberately minimal: no docker-compose, no dynamicconfig, no custom temporal image, no secrets —
# Postgres runs with trust auth because every socket binds 127.0.0.1 and never leaves the host.
# State (db files, pids, logs) persists in $DEVSTACK_STATE; stop never deletes it so a restart
# picks up where the last session left off.
# Health waits (temporal cluster health, pg_isready) are bounded by DEVSTACK_HEALTH_TIMEOUT (default 60 s).
#
#   devstack.sh start          # temporal + set-current-version + postgres
#   devstack.sh stop           # stop kills temporal/keeper and stops postgres; --purge removes
#                              # the postgres container and its data; state files kept
#   devstack.sh status         # one line per component + the exports a driver needs
#   devstack.sh keeper         # migrate + run keeper in the foreground (needs postgres up)
# Fresh stack: `status` reports "keeper: stopped" and the keeper database is empty until
# `keeper` has run once (it runs `admin migrate`, then stays in the foreground; Ctrl-C after
# "schema up to date" is enough). A migrated-only database already satisfies tools/pg-drill-local.sh
# (schema_migrations has 4 rows, verified against postgres:16); order: start, keeper, pg-drill-local.sh.
#   devstack.sh set-current    # re-run just the worker deployment set-current-version step
set -euo pipefail

usage() {
  printf '%s\n' \
    "usage: devstack.sh start|stop [--purge]|status|keeper|set-current" \
    "env: DEVSTACK_STATE TEMPORAL_CLI TEMPORAL_PORT TEMPORAL_UI_PORT DEVSTACK_DEPLOYMENT" \
    "     DEVSTACK_BUILD_ID PG_PORT DEVSTACK_PG_NAME KEEPER_PORT DEVSTACK_PG_MODE PG_BIN" \
    "     DEVSTACK_HEALTH_TIMEOUT" \
    "     TEMPORAL_CLI_VERSION TEMPORAL_CLI_SHA256 DEVSTACK_TEMPORAL_URL_BASE"
}

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# Defaults are overridable so CI and co-located stacks can move ports/paths without editing this file.
DEVSTACK_STATE="${DEVSTACK_STATE:-$REPO_ROOT/.devstack}"
# Captured before the default is applied: ensure_temporal_cli may only hit the network when
# the operator did not choose a CLI themselves.
TEMPORAL_CLI_EXPLICIT="${TEMPORAL_CLI+1}"
TEMPORAL_CLI="${TEMPORAL_CLI:-temporal}"
TEMPORAL_PORT="${TEMPORAL_PORT:-7233}"
TEMPORAL_UI_PORT="${TEMPORAL_UI_PORT:-8233}"
DEVSTACK_DEPLOYMENT="${DEVSTACK_DEPLOYMENT:-d}"
DEVSTACK_BUILD_ID="${DEVSTACK_BUILD_ID:-v1}"
PG_PORT="${PG_PORT:-55432}"
DEVSTACK_PG_NAME="${DEVSTACK_PG_NAME:-plangraph-devstack-pg}"
KEEPER_PORT="${KEEPER_PORT:-18787}"
DEVSTACK_PG_MODE="${DEVSTACK_PG_MODE:-auto}"
DEVSTACK_HEALTH_TIMEOUT="${DEVSTACK_HEALTH_TIMEOUT:-60}"
# Auto-download pin for ensure_temporal_cli; SHA256 may only be overridden together with the
# version, so an arbitrary version never runs on a stale (mismatching) hash.
TEMPORAL_CLI_VERSION="${TEMPORAL_CLI_VERSION:-1.9.1}"
TEMPORAL_CLI_SHA256="${TEMPORAL_CLI_SHA256:-}"
DEVSTACK_TEMPORAL_URL_BASE="${DEVSTACK_TEMPORAL_URL_BASE:-https://github.com/temporalio/cli/releases/download}"

mkdir -p "$DEVSTACK_STATE"
PG_DSN="postgresql://postgres@127.0.0.1:$PG_PORT/keeper"
TEMPORAL_ADDR="127.0.0.1:$TEMPORAL_PORT"

pid_of() { # live pid from a pid file, empty when dead or absent.
  # Optional 2nd arg PATTERN: when /proc/<pid>/cmdline is readable the pid only counts as
  # alive if its command line contains PATTERN (guards against pid reuse across execs);
  # without a readable /proc we fall back to kill -0 only.
  local file="$1" pattern="${2:-}" pid=""
  [ -f "$file" ] && pid="$(cat "$file" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    if [ -n "$pattern" ] && [ -r "/proc/$pid/cmdline" ]; then
      local cmd
      cmd="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
      [[ "$cmd" == *"$pattern"* ]] || return 0
    fi
    printf '%s\n' "$pid"
  fi
}

wait_for() { # wait_for <seconds> <cmd...> — poll once per second until the command succeeds
  local limit="$1"
  shift
  local i
  for ((i = 0; i < limit; i++)); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

docker_available() { command -v docker >/dev/null 2>&1; }

pg_docker_up() {
  docker_available || return 1
  [ -n "$(docker ps -q -f "name=^/${DEVSTACK_PG_NAME}$" 2>/dev/null || true)" ]
}

pg_docker_exists() { # running or merely created — reuse the container instead of hitting name conflicts
  docker_available || return 1
  [ -n "$(docker ps -aq -f "name=^/${DEVSTACK_PG_NAME}$" 2>/dev/null || true)" ]
}

pg_local_up() {
  local pid=""
  [ -f "$DEVSTACK_STATE/pg/postmaster.pid" ] && pid="$(head -n 1 "$DEVSTACK_STATE/pg/postmaster.pid" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

pg_running() { pg_docker_up || pg_local_up; }

resolve_pg_mode() {
  case "$DEVSTACK_PG_MODE" in
    docker|local) printf '%s\n' "$DEVSTACK_PG_MODE" ;;
    auto)
      if docker_available && docker info >/dev/null 2>&1; then printf 'docker\n'; else printf 'local\n'; fi ;;
    *)
      echo "DEVSTACK_PG_MODE must be auto|docker|local (got '$DEVSTACK_PG_MODE')" >&2
      exit 2 ;;
  esac
}

pg_bin_dir() { # pg16 tool dir; PG_BIN overrides for distros that install elsewhere
  local dir="${PG_BIN:-/usr/lib/postgresql/16/bin}"
  [ -x "$dir/initdb" ] && [ -x "$dir/pg_ctl" ] && [ -x "$dir/psql" ] && [ -x "$dir/createdb" ] &&
    printf '%s\n' "$dir"
}

set_current_version() {
  "$TEMPORAL_CLI" worker deployment set-current-version \
    --address "$TEMPORAL_ADDR" \
    --deployment-name "$DEVSTACK_DEPLOYMENT" \
    --build-id "$DEVSTACK_BUILD_ID" \
    --yes
}

ensure_temporal_cli() {
  # Host-agnostic CLI resolution: never touch the network when the CLI is already present.
  if command -v "$TEMPORAL_CLI" >/dev/null 2>&1; then
    return 0
  fi
  if [ -n "$TEMPORAL_CLI_EXPLICIT" ]; then
    # The operator picked the CLI; a bad pick is their problem to fix, not ours to paper
    # over with an unasked-for download.
    echo "temporal: CLI '$TEMPORAL_CLI' not found (set TEMPORAL_CLI)" >&2
    exit 1
  fi
  if [ -x "$DEVSTACK_STATE/bin/temporal" ]; then
    TEMPORAL_CLI="$DEVSTACK_STATE/bin/temporal"
    return 0
  fi
  local os arch url expected actual fetch
  os="$(uname -s)"
  case "$os" in
    Linux) os=linux ;;
    Darwin) os=darwin ;;
    *)
      echo "temporal: unsupported OS '$os' for CLI auto-download (set TEMPORAL_CLI)" >&2
      exit 1 ;;
  esac
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    *)
      echo "temporal: unsupported architecture '$arch' for CLI auto-download (set TEMPORAL_CLI)" >&2
      exit 1 ;;
  esac
  # Explicit TEMPORAL_CLI_SHA256 wins (hermetic tests / non-pinned versions); otherwise the
  # download must match the table for the pinned version — any other version needs both vars.
  if [ -n "$TEMPORAL_CLI_SHA256" ]; then
    expected="$TEMPORAL_CLI_SHA256"
  elif [ "$TEMPORAL_CLI_VERSION" = "1.9.1" ]; then
    case "${os}_${arch}" in
      darwin_amd64) expected=48cdd6c84c56e27ae8e4d9a47052b89289a9c552dee029b49dd312c6fb72873f ;;
      darwin_arm64) expected=41e0425378fcb4fb5766340b97435e20fe47bbff2d7bf644ec2d51f7662b7c56 ;;
      linux_amd64) expected=09a0326a51db84d02735e53542b9ebd8c4758daf47482a9ab0abce15844e60d5 ;;
      linux_arm64) expected=6c57c352d52fc3df34412376fd9ba6f74b7e3ace8e426e6cba8600156d36a145 ;;
      *)
        echo "temporal: no sha256 pin for ${os}_${arch}, set TEMPORAL_CLI_SHA256" >&2
        exit 1 ;;
    esac
  else
    echo "temporal: TEMPORAL_CLI_VERSION=$TEMPORAL_CLI_VERSION is not pinned; set TEMPORAL_CLI_SHA256 too" >&2
    exit 1
  fi
  if command -v curl >/dev/null 2>&1; then
    fetch=curl
  elif command -v wget >/dev/null 2>&1; then
    fetch=wget
  else
    echo "temporal: neither curl nor wget available to fetch the CLI (set TEMPORAL_CLI)" >&2
    exit 1
  fi
  url="${DEVSTACK_TEMPORAL_URL_BASE}/v${TEMPORAL_CLI_VERSION}/temporal_cli_${TEMPORAL_CLI_VERSION}_${os}_${arch}.tar.gz"
  mkdir -p "$DEVSTACK_STATE/bin"
  _tcli_tmp="$(mktemp "$DEVSTACK_STATE/temporal_cli_download.XXXXXX")"
  _tcli_stage="$(mktemp -d "$DEVSTACK_STATE/temporal_cli_stage.XXXXXX")"
  # (globals: the EXIT trap runs after the function's locals are gone) Archive and staging dir go on any exit or signal; bin/temporal is only moved in whole after a
  # verified extract, so an interrupted run cannot leave a partial binary the -x cache check accepts.
  trap 'rm -rf "${_tcli_tmp:-}" "${_tcli_stage:-}"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  echo "temporal: fetching CLI v$TEMPORAL_CLI_VERSION ($url)"
  if [ "$fetch" = curl ]; then
    if ! curl -fsSL "$url" -o "$_tcli_tmp"; then
      rm -f "$_tcli_tmp"
      echo "temporal: download failed (set TEMPORAL_CLI to a local binary)" >&2
      exit 1
    fi
  else
    if ! wget -qO- "$url" >"$_tcli_tmp"; then
      rm -f "$_tcli_tmp"
      echo "temporal: download failed (set TEMPORAL_CLI to a local binary)" >&2
      exit 1
    fi
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$_tcli_tmp" | cut -d' ' -f1)"
  elif command -v shasum >/dev/null 2>&1; then
    actual="$(shasum -a 256 "$_tcli_tmp" | cut -d' ' -f1)"
  else
    rm -f "$_tcli_tmp"
    echo "temporal: no sha256 tool (sha256sum/shasum) to verify the download" >&2
    exit 1
  fi
  if [ "$actual" != "$expected" ]; then
    rm -f "$_tcli_tmp"
    echo "temporal: sha256 mismatch" >&2
    exit 1
  fi
  if ! tar -xzf "$_tcli_tmp" -C "$_tcli_stage" temporal; then
    echo "temporal: archive did not contain a 'temporal' binary" >&2
    exit 1
  fi
  chmod +x "$_tcli_stage/temporal"
  mv -f "$_tcli_stage/temporal" "$DEVSTACK_STATE/bin/temporal"
  rm -rf "$_tcli_tmp" "$_tcli_stage"
  trap - EXIT INT TERM
  TEMPORAL_CLI="$DEVSTACK_STATE/bin/temporal"
  echo "temporal: installed CLI v$TEMPORAL_CLI_VERSION at $TEMPORAL_CLI"
}

start_temporal() {
  ensure_temporal_cli
  local pid
  pid="$(pid_of "$DEVSTACK_STATE/temporal.pid" start-dev)"
  if [ -n "$pid" ]; then
    echo "temporal: already running pid=$pid"
    return 0
  fi
  # start-dev persists to a single db file under the state dir, so restarts keep history.
  # No --headless: we advertise the UI on --ui-port.
  nohup "$TEMPORAL_CLI" server start-dev --ip 127.0.0.1 --port "$TEMPORAL_PORT" \
    --ui-port "$TEMPORAL_UI_PORT" --db-filename "$DEVSTACK_STATE/temporal.db" \
    >"$DEVSTACK_STATE/temporal.log" 2>&1 &
  pid=$!
  printf '%s\n' "$pid" >"$DEVSTACK_STATE/temporal.pid"
  # Health poll doubles as a dead-child watchdog: a crashed start-dev exits its own pid, so we
  # fail fast instead of burning the full timeout.
  local i=0
  while [ "$i" -lt "$DEVSTACK_HEALTH_TIMEOUT" ]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "temporal: server process $pid exited; last log lines:" >&2
      tail -n 20 "$DEVSTACK_STATE/temporal.log" >&2 || true
      rm -f "$DEVSTACK_STATE/temporal.pid"
      exit 1
    fi
    if "$TEMPORAL_CLI" operator cluster health --address "$TEMPORAL_ADDR" >/dev/null 2>&1; then
      echo "temporal: running pid=$pid $TEMPORAL_ADDR ui=127.0.0.1:$TEMPORAL_UI_PORT"
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  echo "temporal: not healthy within ${DEVSTACK_HEALTH_TIMEOUT} s; last log lines:" >&2
  tail -n 20 "$DEVSTACK_STATE/temporal.log" >&2 || true
  kill "$pid" 2>/dev/null || true
  local g=0
  while kill -0 "$pid" 2>/dev/null && [ "$g" -lt 3 ]; do
    sleep 1
    g=$((g + 1))
  done
  # Escalate only while the pid is still alive after the grace period.
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$DEVSTACK_STATE/temporal.pid"
  exit 1
}

start_set_current() {
  # The build-id only becomes settable once a worker has registered it; on a bare devstack that
  # hasn't happened yet, so warn with the exact retry command and let start continue.
  local i
  for ((i = 1; i <= 5; i++)); do
    if set_current_version; then
      echo "worker deployment: current version set ($DEVSTACK_DEPLOYMENT/$DEVSTACK_BUILD_ID)"
      return 0
    fi
    sleep 2
  done
  echo "WARNING: set-current-version failed; the driver hangs without it — once a worker registers build-id '$DEVSTACK_BUILD_ID' re-run: '$TEMPORAL_CLI worker deployment set-current-version --address $TEMPORAL_ADDR --deployment-name $DEVSTACK_DEPLOYMENT --build-id $DEVSTACK_BUILD_ID --yes' (or 'devstack.sh set-current')" >&2
}

start_pg_local() {
  local bin
  if ! bin="$(pg_bin_dir)"; then
    echo "postgres: no pg16 tools found (set PG_BIN to the dir with initdb/pg_ctl/psql/createdb)" >&2
    exit 1
  fi
  if [ ! -f "$DEVSTACK_STATE/pg/PG_VERSION" ]; then
    "$bin/initdb" -D "$DEVSTACK_STATE/pg" -U postgres --auth=trust >/dev/null
  fi
  if ! pg_local_up; then
    # -k points the unix socket into the state dir so nothing leaks to system socket paths.
    "$bin/pg_ctl" -D "$DEVSTACK_STATE/pg" \
      -o "-c listen_addresses=127.0.0.1 -p $PG_PORT -k $DEVSTACK_STATE" \
      -l "$DEVSTACK_STATE/pg.log" -w start >/dev/null
  fi
  if [ "$("$bin/psql" -h 127.0.0.1 -p "$PG_PORT" -U postgres -d postgres -Atc \
      "SELECT 1 FROM pg_database WHERE datname = 'keeper'" 2>/dev/null || true)" != "1" ]; then
    "$bin/createdb" -h 127.0.0.1 -p "$PG_PORT" -U postgres keeper
  fi
  echo "postgres: running (local) 127.0.0.1:$PG_PORT"
}

start_postgres() {
  local mode
  mode="$(resolve_pg_mode)"
  if [ "$mode" = docker ]; then
    if ! docker_available; then
      echo "postgres: DEVSTACK_PG_MODE=docker but docker is not installed (try DEVSTACK_PG_MODE=local)" >&2
      exit 1
    fi
    if pg_docker_up; then
      echo "postgres: already running (docker $DEVSTACK_PG_NAME)"
      return 0
    fi
    local created=0
    if pg_docker_exists; then
      docker start "$DEVSTACK_PG_NAME" >/dev/null
    else
      # trust auth + 127.0.0.1-only port publish: dev state, safe because nothing binds outward.
      # A failed run can leave a created container behind; remove it so the next start doesn't
      # wrongly docker start it.
      if ! docker run -d --name "$DEVSTACK_PG_NAME" --memory 1g \
        -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=keeper \
        -p "127.0.0.1:$PG_PORT:5432" postgres:16 >/dev/null; then
        docker rm -f -v "$DEVSTACK_PG_NAME" >/dev/null 2>&1 || true
        echo "postgres: docker run failed; removed any partially created container" >&2
        exit 1
      fi
      created=1
    fi
    if wait_for "$DEVSTACK_HEALTH_TIMEOUT" docker exec "$DEVSTACK_PG_NAME" pg_isready -U postgres -d keeper; then
      echo "postgres: running (docker $DEVSTACK_PG_NAME) 127.0.0.1:$PG_PORT"
    else
      echo "postgres: container $DEVSTACK_PG_NAME never became ready within ${DEVSTACK_HEALTH_TIMEOUT} s" >&2
      # Only a container this invocation created is destroyed; a reused one is stopped so its data survives.
      if [ "$created" = 1 ]; then
        docker rm -f -v "$DEVSTACK_PG_NAME" >/dev/null 2>&1 || true
      else
        docker stop "$DEVSTACK_PG_NAME" >/dev/null 2>&1 || true
      fi
      exit 1
    fi
  else
    start_pg_local
  fi
}

cmd_start() {
  resolve_pg_mode >/dev/null # fail on a bad DEVSTACK_PG_MODE before anything is started
  start_temporal
  start_set_current
  start_postgres
}

stop_pidfile() { # TERM, grace 10 s, then KILL; the pid file goes away either way.
                   # Optional 2nd arg is the pid_of cmdline PATTERN.
  local file="$1" pattern="${2:-}" pid
  pid="$(pid_of "$file" "$pattern")"
  if [ -z "$pid" ]; then
    rm -f "$file"
    return 0
  fi
  kill "$pid" 2>/dev/null || true
  local i=0
  while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 10 ]; do
    sleep 1
    i=$((i + 1))
  done
  # Only escalate when the pid is still alive: it may have exited and been reused.
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$file"
}

cmd_stop() {
  local purge=0
  [ "${1:-}" = "--purge" ] && purge=1
  stop_pidfile "$DEVSTACK_STATE/temporal.pid" start-dev
  stop_pidfile "$DEVSTACK_STATE/keeper.pid" plangraph.keeper.daemon
  if pg_docker_exists; then
    if [ "$purge" = 1 ]; then
      # -v also removes the container's anonymous data volume so --purge does not leak it.
      docker rm -f -v "$DEVSTACK_PG_NAME" >/dev/null 2>&1 || true
    else
      # Stop only: the container (and its data) survives for the next start's docker reuse.
      docker stop "$DEVSTACK_PG_NAME" >/dev/null 2>&1 || true
    fi
  fi
  if pg_local_up; then
    local bin
    if bin="$(pg_bin_dir)"; then
      "$bin/pg_ctl" -D "$DEVSTACK_STATE/pg" -m fast stop
    else
      # Leaving the cluster running beats stranding it unstopgable; state files are never touched.
      echo "postgres: local cluster running but pg tools missing (set PG_BIN)" >&2
    fi
  fi
  exit 0
}

cmd_status() {
  local ok=0 pid
  pid="$(pid_of "$DEVSTACK_STATE/temporal.pid" start-dev)"
  if [ -n "$pid" ]; then
    echo "temporal: running pid=$pid $TEMPORAL_ADDR ui=127.0.0.1:$TEMPORAL_UI_PORT"
  else
    echo "temporal: stopped"
    ok=1
  fi
  if pg_docker_up; then
    echo "postgres: running (docker $DEVSTACK_PG_NAME) 127.0.0.1:$PG_PORT"
  elif pg_local_up; then
    echo "postgres: running (local) 127.0.0.1:$PG_PORT"
  else
    echo "postgres: stopped"
    ok=1
  fi
  pid="$(pid_of "$DEVSTACK_STATE/keeper.pid" plangraph.keeper.daemon)"
  if [ -n "$pid" ]; then
    echo "keeper: running pid=$pid 127.0.0.1:$KEEPER_PORT"
  else
    echo "keeper: stopped"
  fi
  # Emitted even when stopped so a driver can eval them before starting the stack.
  echo "export PG_KEEPER_BACKEND=postgres"
  echo "export PG_DSN=$PG_DSN"
  echo "export TEMPORAL_ADDR=$TEMPORAL_ADDR"
  return "$ok"
}

cmd_keeper() {
  pg_running || { echo "keeper: postgres is not up — run devstack.sh start first" >&2; exit 3; }
  local file="$DEVSTACK_STATE/keeper.json"
  # umask 077 before the redirect: the file embeds the trust-auth dsn, keep it owner-only.
  (
    umask 077
    DEVSTACK_STATE="$DEVSTACK_STATE" KEEPER_PORT="$KEEPER_PORT" python3 - "$file" "$PG_DSN" <<'PY'
import json, os, sys
with open(sys.argv[1], "w") as fh:
    fh.write(json.dumps({
        "backend": "postgres",
        "dsn": sys.argv[2],
        "bind": f"127.0.0.1:{os.environ['KEEPER_PORT']}",
        "snapshot": f"{os.environ['DEVSTACK_STATE']}/plan.snapshot.json",
    }))
PY
  )
  chmod 600 "$file"
  PYTHONPATH="$REPO_ROOT/src" python3 -m plangraph.keeper.cli --config "$file" admin migrate
  export PG_KEEPER_BACKEND=postgres PG_DSN="$PG_DSN"
  # exec so the pid file matches the foreground process the daemon actually runs as; write it
  # only right before the exec so the pid is never claimed while this is still bash devstack.sh.
  printf '%s\n' "$$" >"$DEVSTACK_STATE/keeper.pid"
  export PYTHONPATH="$REPO_ROOT/src"
  exec python3 -m plangraph.keeper.daemon --config "$file" --host 127.0.0.1 --port "$KEEPER_PORT"
}

case "${1:-}" in
  start) cmd_start ;;
  stop) shift; cmd_stop "$@" ;;
  status) cmd_status ;;
  keeper) cmd_keeper ;;
  set-current) ensure_temporal_cli; set_current_version ;;
  -h|--help|help) usage ;;
  "") usage; exit 2 ;;
  *) echo "unknown command: $1" >&2; usage; exit 2 ;;
esac
