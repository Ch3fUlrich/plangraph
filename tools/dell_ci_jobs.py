"""Private, disposable service and build scripts for the Dell CI driver."""

RUST = {
    "rust": """cd rust
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo check --target x86_64-pc-windows-gnu -p pg-worker-core -p pg-spawnd --all-targets
cargo build -p pg-jail
PG_JAIL_BIN="$CARGO_TARGET_DIR/debug/pg-jail" cargo test""",
    "rust-worker": """cd rust/worker
temporal server start-dev --ip 127.0.0.1 --port 17233 > "$scratch/temporal.log" 2>&1 &
server=$!
ready=0
for i in $(seq 1 30); do
    if temporal operator cluster health --address 127.0.0.1:17233 >/dev/null 2>&1; then ready=1; break; fi
    sleep 1
done
test "$ready" = 1
cargo fmt --check
cargo clippy --all-targets -- -D warnings
PG_TEMPORAL_ADDR=http://127.0.0.1:17233 PG_JAIL=off PG_TEST_ALLOW_UNJAILED=1 cargo test""",
    "rust-spike": "cargo test --manifest-path rust/spikes/temporal-spike/Cargo.toml --test replay",
}

POSTGRES = """password=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export POSTGRES_PASSWORD="$password"
docker run -d --name "$container" -e POSTGRES_PASSWORD -e POSTGRES_DB=keeper -p 127.0.0.1:0:5432 postgres:16 >/dev/null
ready=0
for i in $(seq 1 50); do
    if docker exec "$container" pg_isready -U postgres >/dev/null 2>&1; then ready=1; break; fi
    sleep 1
done
test "$ready" = 1
port=$(docker port "$container" 5432/tcp)
port=${port##*:}
PG_DSN=$(printf '%s://%s:%s@%s:%s/%s' postgresql postgres "$password" 127.0.0.1 "$port" keeper)
export PG_DSN
export PG_KEEPER_BACKEND=postgres PG_CONTRACT_ABORT_ON_TIMEOUT=1
export PG_CONTRACT_LATENCY_DIR="$scratch/latency"
FILES=$(grep -lE 'keeper_util|door_util|plangraph\\.keeper|from plangraph import' tests/test_*.py | xargs -r grep -L sqlite3 | tr '\\n' ' ' || :)
python3 -m pytest -q -n auto --dist loadgroup --durations=30 tests/contract tests/test_keeper_pg_unit.py tests/test_keeper_pg_parity.py tests/test_keeper_pg_copy.py tests/test_projector_ddl.py $FILES
"""


def script(name, scratch):
    """All exits (including TERM from timeout) release owned resources."""
    body = POSTGRES if name == "pytest-postgres" else RUST[name]
    return f"""set -eu
set +x
scratch={scratch}
container=dell-ci-${{scratch##*/}}
server=
cleanup() {{
    code=$?
    trap - EXIT HUP INT TERM
    if [ -n "$server" ]; then kill "$server" 2>/dev/null || :; wait "$server" 2>/dev/null || :; fi
    if [ '{name}' = pytest-postgres ]; then
        docker rm -f "$container" >/dev/null 2>&1 || code=1
    fi
    rm -rf -- "$scratch" || code=1
    exit "$code"
}}
trap cleanup EXIT
trap 'exit 143' HUP INT TERM
mkdir -m 700 "$scratch"
export CARGO_TARGET_DIR="$scratch/target" CARGO_BUILD_JOBS=4
{body}
"""
