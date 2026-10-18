#!/usr/bin/env bash
# pg-drill-local.sh — dev keeper ONLY: dump the dev keeper database, restore it into a
# throwaway postgres:16 container, and compare per-table row counts source vs restore.
# Loopback-only: every socket
# and port binding is 127.0.0.1, and the container uses trust auth because it is destroyed
# unconditionally on exit. The restored copy is trust-auth on 127.0.0.1, so it is readable
# by any local user — never point this script at a non-dev database. The drill fails if any source table is missing from the restore,
# if any table appears in the restore but not the source, if any count differs, or if the
# restore is vacuous (0 tables or 0 total rows) — an empty restore matching an empty check
# is never a pass. Health waits are bounded by DRILL_HEALTH_TIMEOUT (default 60 s).
#
# Usage sequence: client tools (pg_dump, psql, pg_restore, pg_isready) are taken from PATH,
# and any that is missing runs from the postgres:16 image via docker (host network, dump
# dir mounted at the same path), so the host needs only docker. Sequence on a fresh stack:
#   tools/devstack.sh start
#   tools/devstack.sh keeper   # once: runs `plangraph.keeper.cli admin migrate`, which
#                              # creates the keeper schema with a populated schema_migrations
#                              # table (4 rows at schema version 4), then stays in the
#                              # foreground — stop it with Ctrl-C after "schema up to date"
#   tools/pg-drill-local.sh
# A migrated-only database passes the >0 total rows check because schema_migrations alone
# has rows (verified against a real postgres:16 with admin migrate: 21 tables in one
# k_<hash> schema, 4 schema_migrations rows) and because the drill covers every
# non-system schema, not only public.
set -euo pipefail

usage() {
  printf '%s\n' \
    "usage: pg-drill-local.sh" \
    "env: SRC_DSN PG_PORT DRILL_PORT DRILL_NAME DRILL_MEMORY DRILL_DUMP_DIR DRILL_HEALTH_TIMEOUT" \
    "dev keeper only: SRC_DSN must be postgresql://[user[:pw]@]127.0.0.1|localhost:<PG_PORT>/db" \
    "the restored copy is trust-auth on 127.0.0.1 and readable by any local user"
}

case "${1:-}" in
  -h|--help|help) usage; exit 0 ;;
  "") ;;
  *) usage >&2; exit 2 ;;
esac

SRC_DSN="${SRC_DSN:-postgresql://postgres@127.0.0.1:${PG_PORT:-55432}/keeper}"

# Dev-target guard: the restored copy is trust-auth on 127.0.0.1 and this script dumps
# real data, so any SRC_DSN that is not the loopback dev-keeper port is refused before
# anything runs. The DSN is parsed with bash expansions only — never eval'd, never shown.
assert_dev_target() { # assert_dev_target <dsn> — 0 if the DSN is the dev target, 1 otherwise
  local dsn="$1" rest authority hostport host port
  case "$dsn" in
    postgresql://*) rest="${dsn#postgresql://}" ;;
    postgres://*) rest="${dsn#postgres://}" ;;
    *) return 1 ;;
  esac
  # a query string can override host/port/hostaddr in libpq, so any query is refused
  case "$rest" in *\?*) return 1 ;; esac
  authority="${rest%%/*}"
  hostport="${authority##*@}"
  case "$hostport" in
    *:*) ;;
    *) return 1 ;;
  esac
  port="${hostport##*:}"
  host="${hostport%:*}"
  [ -n "$host" ] || return 1
  case "$port" in
    '' | *[!0-9]*) return 1 ;;
  esac
  [ "$port" = "${PG_PORT:-55432}" ] || return 1
  case "$host" in
    127.0.0.1 | localhost) return 0 ;;
    *) return 1 ;;
  esac
}

if ! assert_dev_target "$SRC_DSN"; then
  echo "PG-DRILL refused: non-dev target; a LIVE drill needs a separate GO and a password-auth drill container"
  exit 2
fi

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

pgtool() { # pgtool <tool> args... : host tool if on PATH, else the same tool from the postgres:16 image
  local tool="$1"; shift
  if command -v "$tool" >/dev/null 2>&1; then "$tool" "$@"; return; fi
  # --network host: reaches the 127.0.0.1-published source and drill ports from inside the
  # container.  -v "$DRILL_DUMP_DIR:$DRILL_DUMP_DIR": the dump dir is mounted at the
  # identical path so -f / pg_restore file args resolve the same way in container and host.
  # -i: passes stdin through, so table_count's piped query works.  --user: the container
  # writes into the host-owned 0700 mktemp dir, so it must run as the host uid/gid.
  docker run --rm -i --memory "$DRILL_MEMORY" --network host --user "$(id -u):$(id -g)" -v "$DRILL_DUMP_DIR:$DRILL_DUMP_DIR" postgres:16 "$tool" "$@"
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

list_tables() { # list_tables <dsn> — one "schema<TAB>table" line per BASE TABLE in every
                # non-system schema (the keeper lives in a derived k_<hash> schema, not public)
  pgtool psql "$1" -A -t -F $'\t' -c \
    "SELECT table_schema, table_name FROM information_schema.tables WHERE table_type='BASE TABLE' AND table_schema <> 'information_schema' AND table_schema NOT LIKE 'pg\_%' ORDER BY 1,2" \
    </dev/null
}

table_count() { # table_count <dsn> <schema> <table> — exact count(*); psql quotes :"sch"/:"tbl" only via STDIN, not -c
  printf '%s\n' 'SELECT count(*) FROM :"sch".:"tbl"' |
    pgtool psql "$1" -A -t -v ON_ERROR_STOP=1 -v "sch=$2" -v "tbl=$3"
}

t0="$(date +%s)"

pgtool pg_dump -Fc "$SRC_DSN" -f "$DUMP_FILE" </dev/null || fail "pg_dump against the source failed"

# A crashed earlier drill leaves a name-occupied container behind; remove it before the fresh run.
docker rm -f "$DRILL_NAME" >/dev/null 2>&1 || true
docker run -d --name "$DRILL_NAME" --memory "$DRILL_MEMORY" \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -p "127.0.0.1:$DRILL_PORT:5432" postgres:16 >/dev/null || fail "docker run failed"

wait_for "$DRILL_HEALTH_TIMEOUT" pgtool pg_isready -h 127.0.0.1 -p "$DRILL_PORT" -U postgres </dev/null ||
  fail "drill container never became ready within ${DRILL_HEALTH_TIMEOUT} s"

pgtool psql "postgresql://postgres@127.0.0.1:$DRILL_PORT/postgres" -c 'CREATE DATABASE keeper_drill' >/dev/null </dev/null ||
  fail "createdb keeper_drill failed"
pgtool pg_restore --no-owner -d "$RESTORED_DSN" "$DUMP_FILE" </dev/null || fail "pg_restore into keeper_drill failed"

compare_counts() {
  local src_tables restored_tables line s t name sc dc
  src_tables="$(list_tables "$SRC_DSN")" || fail "listing source tables failed"
  restored_tables="$(list_tables "$RESTORED_DSN")" || fail "listing restored tables failed"

  if [ -z "$restored_tables" ]; then
    echo "PG-DRILL fail vacuous: restored database has 0 tables - the source database holds no keeper data yet; start postgres with tools/devstack.sh start, then run tools/devstack.sh keeper once so it migrates (a migrated database is enough; stop the keeper afterwards), or point SRC_DSN at a seeded database"
    exit 1
  fi

  # Membership keys are full "schema<TAB>table" lines; the displayed name drops the
  # schema only for public, so output stays `table t1 10` for public tables.
  while IFS=$'\t' read -r s t; do
    [ -n "$t" ] || continue
    line="$s"$'\t'"$t"
    if [ "$s" = public ]; then name="$t"; else name="$s.$t"; fi
    printf '%s\n' "$restored_tables" | grep -Fxq -- "$line" ||
      fail "table $name missing from restored database"
    sc="$(table_count "$SRC_DSN" "$s" "$t")" || fail "counting $name in the source failed"
    dc="$(table_count "$RESTORED_DSN" "$s" "$t")" || fail "counting $name in the restore failed"
    [ "$sc" = "$dc" ] || fail "count mismatch for table $name: source=$sc restored=$dc"
    echo "table $name $dc"
    TOTAL_ROWS=$((TOTAL_ROWS + dc))
  done <<<"$src_tables"

  while IFS=$'\t' read -r s t; do
    [ -n "$t" ] || continue
    line="$s"$'\t'"$t"
    if [ "$s" = public ]; then name="$t"; else name="$s.$t"; fi
    printf '%s\n' "$src_tables" | grep -Fxq -- "$line" ||
      fail "unexpected table $name present in restore but not in source"
  done <<<"$restored_tables"

  if [ "$TOTAL_ROWS" = 0 ]; then
    echo "PG-DRILL fail vacuous: restored database has 0 total rows - the source database holds no keeper data yet; start postgres with tools/devstack.sh start, then run tools/devstack.sh keeper once so it migrates (a migrated database is enough; stop the keeper afterwards), or point SRC_DSN at a seeded database"
    exit 1
  fi
}

compare_counts

rto_s="$(( $(date +%s) - t0 ))"
echo "PG-DRILL ok rto_s=$rto_s rows=$TOTAL_ROWS"
