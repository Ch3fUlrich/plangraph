#!/usr/bin/env bash
# pg-drill-local.sh — dump the dev keeper database, restore it into a throwaway postgres:16
# container, and compare per-table row counts source vs restore. Loopback-only: every socket
# and port binding is 127.0.0.1, and the container uses trust auth because it is destroyed
# unconditionally on exit. The drill fails if any source table is missing from the restore,
# if any table appears in the restore but not the source, if any count differs, or if the
# restore is vacuous (0 tables or 0 total rows) — an empty restore matching an empty check
# is never a pass. Health waits are bounded by DRILL_HEALTH_TIMEOUT (default 60 s).
set -euo pipefail

usage() {
  printf '%s\n' \
    "usage: pg-drill-local.sh" \
    "env: SRC_DSN PG_PORT DRILL_PORT DRILL_NAME DRILL_MEMORY DRILL_DUMP_DIR DRILL_HEALTH_TIMEOUT"
}

case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
  "") ;;
  *) usage >&2; exit 2 ;;
esac

SRC_DSN="${SRC_DSN:-postgresql://postgres@127.0.0.1:${PG_PORT:-55432}/keeper}"
DRILL_PORT="${DRILL_PORT:-55433}"
DRILL_NAME="${DRILL_NAME:-plangraph-pgdrill}"
DRILL_MEMORY="${DRILL_MEMORY:-1g}"
DRILL_HEALTH_TIMEOUT="${DRILL_HEALTH_TIMEOUT:-60}"
# Only a dump dir this script created is deleted on exit; a caller-provided one is kept.
DRILL_DUMP_OWNED=0
if [ -z "${DRILL_DUMP_DIR:-}" ]; then
  DRILL_DUMP_DIR="$(mktemp -d)"
  DRILL_DUMP_OWNED=1
fi
RESTORED_DSN="postgresql://postgres@127.0.0.1:$DRILL_PORT/keeper_drill"
DUMP_FILE="$DRILL_DUMP_DIR/keeper.dump"
TOTAL_ROWS=0

fail() { # fail <msg>: print one PG-DRILL fail line and exit 1
  echo "PG-DRILL fail $*"
  exit 1
}

cleanup() {
  docker rm -f "$DRILL_NAME" >/dev/null 2>&1 || true
  if [ "$DRILL_DUMP_OWNED" = 1 ]; then
    rm -rf "$DRILL_DUMP_DIR"
  fi
}
trap cleanup EXIT
# EXIT alone does not fire on a signal kill; re-raise via exit so cleanup always runs.
trap 'exit 130' INT
trap 'exit 143' TERM

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

list_tables() { # list_tables <dsn> — user (BASE TABLE) names in schema public, sorted
  psql "$1" -A -t -c \
    "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name"
}

table_count() { # table_count <dsn> <table> — exact count(*); psql quotes :"tbl" only via STDIN, not -c
  printf '%s\n' 'SELECT count(*) FROM public.:"tbl"' |
    psql "$1" -A -t -v ON_ERROR_STOP=1 -v "tbl=$2"
}

t0="$(date +%s)"

pg_dump -Fc "$SRC_DSN" -f "$DUMP_FILE" || fail "pg_dump against the source failed"

# A crashed earlier drill leaves a name-occupied container behind; remove it before the fresh run.
docker rm -f "$DRILL_NAME" >/dev/null 2>&1 || true
docker run -d --name "$DRILL_NAME" --memory "$DRILL_MEMORY" \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -p "127.0.0.1:$DRILL_PORT:5432" postgres:16 >/dev/null || fail "docker run failed"

wait_for "$DRILL_HEALTH_TIMEOUT" pg_isready -h 127.0.0.1 -p "$DRILL_PORT" -U postgres ||
  fail "drill container never became ready within ${DRILL_HEALTH_TIMEOUT} s"

psql "postgresql://postgres@127.0.0.1:$DRILL_PORT/postgres" -c 'CREATE DATABASE keeper_drill' >/dev/null ||
  fail "createdb keeper_drill failed"
pg_restore --no-owner -d "$RESTORED_DSN" "$DUMP_FILE" || fail "pg_restore into keeper_drill failed"

compare_counts() {
  local src_tables restored_tables t sc dc
  src_tables="$(list_tables "$SRC_DSN")" || fail "listing source tables failed"
  restored_tables="$(list_tables "$RESTORED_DSN")" || fail "listing restored tables failed"

  if [ -z "$restored_tables" ]; then
    echo "PG-DRILL fail vacuous: restored database has 0 tables"
    exit 1
  fi

  while IFS= read -r t; do
    [ -n "$t" ] || continue
    printf '%s\n' "$restored_tables" | grep -Fxq -- "$t" ||
      fail "table $t missing from restored database"
    sc="$(table_count "$SRC_DSN" "$t")" || fail "counting $t in the source failed"
    dc="$(table_count "$RESTORED_DSN" "$t")" || fail "counting $t in the restore failed"
    [ "$sc" = "$dc" ] || fail "count mismatch for table $t: source=$sc restored=$dc"
    echo "table $t $dc"
    TOTAL_ROWS=$((TOTAL_ROWS + dc))
  done <<<"$src_tables"

  while IFS= read -r t; do
    [ -n "$t" ] || continue
    printf '%s\n' "$src_tables" | grep -Fxq -- "$t" ||
      fail "unexpected table $t present in restore but not in source"
  done <<<"$restored_tables"

  if [ "$TOTAL_ROWS" = 0 ]; then
    echo "PG-DRILL fail vacuous: restored database has 0 total rows"
    exit 1
  fi
}

compare_counts

rto_s="$(( $(date +%s) - t0 ))"
echo "PG-DRILL ok rto_s=$rto_s rows=$TOTAL_ROWS"
