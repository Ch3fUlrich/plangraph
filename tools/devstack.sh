#!/usr/bin/env bash
# devstack.sh — drive the local development stack defined by deploy/dev/compose.yml
# (postgres + temporal + temporal-ui, keeper endpoint). Docker is required for
# everything except `init`; this script never prints a secret.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEV_DIR="$REPO_ROOT/deploy/dev"
ENV_FILE="$DEV_DIR/.env"
COMPOSE=(docker compose -p plangraph-dev -f "$DEV_DIR/compose.yml" --env-file "$ENV_FILE")

# Fallback service list for the failure dump in `up`, used when
# `docker compose config --services` is unavailable.
FALLBACK_SERVICES="pg temporal-schema temporal temporal-namespace temporal-ui"

usage() {
    cat >&2 <<'EOF'
usage: tools/devstack.sh {init,up,down,reset,status,logs,keeper}
  init                 create deploy/dev/.env (password never printed, file never overwritten)
  up                   start the stack and wait until healthy
  down                 stop the stack (volumes kept)
  reset --yes          destroy the stack AND its volumes (data is lost)
  status               show containers and endpoints
  logs [service...]    follow logs (--tail 200)
  keeper               run the working-tree keeper against the dev Postgres
EOF
}

# Minimal KEY=VALUE parser for the env file. Never `source` it: the file is
# operator-editable and executing it would run arbitrary code.
env_get() {
    local key="$1" file="$2" line value
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            "$key="*)
                value="${line#*=}"
                value="${value%$'\r'}"
                value="${value%\"}"
                value="${value#\"}"
                value="${value%\'}"
                value="${value#\'}"
                printf '%s' "$value"
                return 0
                ;;
        esac
    done < "$file"
    return 1
}

require_env_file() {
    if [ ! -f "$ENV_FILE" ]; then
        echo "no $ENV_FILE — run init first: tools/devstack.sh init" >&2
        exit 2
    fi
}

gen_password() {
    local secret
    if secret=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))' 2>/dev/null) &&
        [ -n "$secret" ]; then
        printf '%s' "$secret"
    elif secret=$(openssl rand -base64 24 2>/dev/null | tr -d '/+=') &&
        [ -n "$secret" ]; then
        printf '%s' "$secret"
    else
        echo "cannot generate a password: need python3 or openssl" >&2
        exit 1
    fi
}

cmd_init() {
    if [ -f "$ENV_FILE" ]; then
        echo "already initialised"
        exit 0
    fi
    umask 077
    mkdir -p "$DEV_DIR"
    local pg_password
    pg_password="$(gen_password)"
    # The redirection creates the file under umask 077; $pg_password is never echoed.
    printf 'PG_USER=%s\nPG_PASSWORD=%s\nPG_PORT=%s\nTEMPORAL_PORT=%s\nTEMPORAL_UI_PORT=%s\nKEEPER_PORT=%s\n' \
        "plangraph" "$pg_password" "55432" "7233" "8233" "18787" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    local mode
    mode=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE" 2>/dev/null || echo unknown)
    if [ "$mode" != "600" ]; then
        echo "warning: $ENV_FILE reports mode $mode, expected 600" >&2
    fi
    echo "wrote $ENV_FILE (mode 600)"
}

cmd_up() {
    require_env_file
    if ! docker info >/dev/null 2>&1; then
        echo "docker is not available (daemon not reachable)" >&2
        exit 3
    fi
    local rc=0
    "${COMPOSE[@]}" up -d --wait --wait-timeout 180 || rc=$?
    if [ "$rc" -ne 0 ]; then
        # Keep every container as it is (no down/rm/kill/stop) and dump what
        # there is so the failure can be diagnosed.
        echo "compose up failed (exit $rc); containers left as they are" >&2
        echo "--- containers ---" >&2
        "${COMPOSE[@]}" ps -a || true
        echo "--- logs (last 80 lines per service) ---" >&2
        local services svc
        services=$("${COMPOSE[@]}" config --services 2>/dev/null) ||
            services="$FALLBACK_SERVICES"
        for svc in $services; do
            echo "--- $svc ---" >&2
            "${COMPOSE[@]}" logs --tail 80 "$svc" 2>/dev/null || true
        done
        echo "read them again with: tools/devstack.sh logs <service>" >&2
        exit "$rc"
    fi
}

cmd_down() {
    require_env_file
    "${COMPOSE[@]}" down
}

cmd_reset() {
    require_env_file
    if [ "${1:-}" != "--yes" ]; then
        cat >&2 <<EOF
reset destroys the data of the plangraph-dev stack:
  - the named volumes of the stack (Postgres data for keeper/temporal/temporal_visibility)
  - all containers of the stack, including orphan containers
Nothing is deleted without confirmation. Re-run with:
  tools/devstack.sh reset --yes
EOF
        exit 2
    fi
    # The ONLY place `down -v` may appear.
    "${COMPOSE[@]}" down -v --remove-orphans
}

cmd_status() {
    require_env_file
    "${COMPOSE[@]}" ps -a || true
    local pg_port temporal_port ui_port keeper_port
    pg_port="$(env_get PG_PORT "$ENV_FILE" || true)"
    temporal_port="$(env_get TEMPORAL_PORT "$ENV_FILE" || true)"
    ui_port="$(env_get TEMPORAL_UI_PORT "$ENV_FILE" || true)"
    keeper_port="$(env_get KEEPER_PORT "$ENV_FILE" || true)"
    echo "pg:       127.0.0.1:${pg_port:-<unset>}"
    echo "temporal: 127.0.0.1:${temporal_port:-<unset>}"
    echo "ui:       http://127.0.0.1:${ui_port:-<unset>}"
    echo "keeper:   127.0.0.1:${keeper_port:-<unset>}"
}

cmd_logs() {
    require_env_file
    exec "${COMPOSE[@]}" logs --tail 200 -f "$@"
}

cmd_keeper() {
    require_env_file
    local pg_port keeper_port
    pg_port="$(env_get PG_PORT "$ENV_FILE" || true)"
    keeper_port="$(env_get KEEPER_PORT "$ENV_FILE" || true)"
    keeper_port="${keeper_port:-18787}"
    if [ -z "$pg_port" ]; then
        echo "$ENV_FILE has no PG_PORT — re-run: tools/devstack.sh init" >&2
        exit 2
    fi
    if ! nc -z 127.0.0.1 "$pg_port"; then
        echo "stack is not up, run: tools/devstack.sh up" >&2
        exit 3
    fi

    umask 077
    # Build keeper.json with python3 json.dumps so the URL-encoded password and
    # the JSON quoting are right; the dsn is never echoed.
    DEV_DIR="$DEV_DIR" ENV_FILE_PATH="$ENV_FILE" KEEPER_PORT="$keeper_port" python3 - <<'PY'
import json
import os
import urllib.parse

dev_dir = os.environ["DEV_DIR"]
env_file = os.environ["ENV_FILE_PATH"]
keeper_port = os.environ["KEEPER_PORT"]

values = {}
with open(env_file, encoding="utf-8") as fh:
    for line in fh:
        line = line.rstrip("\r\n")
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()

dsn = "postgresql://{}:{}@127.0.0.1:{}/keeper".format(
    urllib.parse.quote(values["PG_USER"], safe=""),
    urllib.parse.quote(values["PG_PASSWORD"], safe=""),
    urllib.parse.quote(values["PG_PORT"], safe=""),
)
config = {
    "backend": "postgres",
    "dsn": dsn,
    "bind": f"127.0.0.1:{keeper_port}",
    "snapshot": os.path.join(dev_dir, "plan.snapshot.json"),
}
# Created under the shell's umask 077; os.open gives it the exact 0600 even if
# the file already existed with another mode.
fd = os.open(os.path.join(dev_dir, "keeper.json"),
             os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(config, fh, indent=2)
    fh.write("\n")
PY
    # The keeper refuses any config mode other than 600; chmod re-applies the
    # mode in case a previous run left the file with another one.
    chmod 600 "$DEV_DIR/keeper.json"

    # `python3 -m plangraph.keeper.cli --config ... admin migrate` applies the
    # schema (the CLI verbs the daemon needs are there too). Not fatal: if the
    # daemon brings the schema up itself, this failure can be ignored; re-run
    # the command above manually if it complains.
    PYTHONPATH="$REPO_ROOT/src" python3 -m plangraph.keeper.cli \
        --config "$DEV_DIR/keeper.json" admin migrate ||
        echo "keeper admin migrate failed; if the daemon does not apply the schema, run: PYTHONPATH=src python3 -m plangraph.keeper.cli --config $DEV_DIR/keeper.json admin migrate" >&2

    PYTHONPATH="$REPO_ROOT/src" exec python3 -m plangraph.keeper.daemon \
        --config "$DEV_DIR/keeper.json" --host 127.0.0.1 --port "$keeper_port"
}

action="${1:-}"
shift || true
case "$action" in
    init) cmd_init ;;
    up) cmd_up ;;
    down) cmd_down ;;
    reset) cmd_reset "$@" ;;
    status) cmd_status ;;
    logs) cmd_logs "$@" ;;
    keeper) cmd_keeper ;;
    *) usage; exit 2 ;;
esac
