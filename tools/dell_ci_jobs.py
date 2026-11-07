"""Private, disposable service and build scripts for the Dell CI driver."""
import shlex


def sandbox_argv(checkout, scratch, toolchain, cargo_bin, command):
    """Pure capability list: no inherited environment or host home mounts."""
    argv = ["/usr/bin/bwrap", "--unshare-user", "--unshare-pid",
            "--unshare-ipc", "--die-with-parent", "--new-session", "--clearenv"]
    for path in ("/usr", "/bin", "/lib", "/lib64", "/etc"):
        argv += ["--ro-bind", path, path]
    argv += ["--ro-bind", toolchain, "/toolchain", "--ro-bind", cargo_bin, "/cargo-bin",
             "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
             "--tmpfs", "/home", "--dir", "/ci-home",
             "--bind", checkout, "/work", "--bind", scratch, "/scratch",
             "--chdir", "/work"]
    env = {"HOME": "/ci-home", "PATH": "/scratch/bin:/toolchain/bin:/cargo-bin:/usr/bin:/bin",
           "CARGO_HOME": "/scratch/cargo", "CARGO_TARGET_DIR": "/scratch/target",
           "RUSTUP_HOME": "/scratch/rustup", "XDG_CACHE_HOME": "/scratch/cache",
           "PYTHONPATH": "/work/src", "CARGO_BUILD_JOBS": "4", "LANG": "C.UTF-8"}
    for key, value in env.items():
        argv += ["--setenv", key, value]
    return argv + ["sh", "-c", command]


def isolated_script(command, source, job, *, postgres=False):
    """Trusted outer lifecycle; only the inner command sees PR-controlled files."""
    setup = POSTGRES.split('FILES=', 1)[0]
    if postgres:
        command = ('export PG_DSN="$1" PG_KEEPER_BACKEND=postgres '
                   'PG_CONTRACT_ABORT_ON_TIMEOUT=1 PG_CONTRACT_LATENCY_DIR=/scratch/latency\n'
                   + command)
    argv = sandbox_argv("CHECKOUT", "SCRATCH", "TOOLCHAIN", "CARGOBIN", command)
    invocation = shlex.join(argv)
    for token, expression in {"CHECKOUT": '"$scratch/checkout"',
                              "SCRATCH": '"$scratch/data"',
                              "TOOLCHAIN": '"$toolchain"',
                              "CARGOBIN": '"$HOME/.cargo/bin"'}.items():
        invocation = invocation.replace(token, expression)
    if postgres:
        invocation += ' sh "$PG_DSN"'
    return f'''set -eu
scratch={job}
container=dell-ci-${{scratch##*/}}-${{scratch%/*}}
container=$(printf '%s' "$container" | tr / -)
container_id=
cleanup() {{
    code=$?
    trap - EXIT HUP INT TERM
    if [ -n "$container_id" ]; then docker rm -f "$container_id" >/dev/null 2>&1 || code=1; fi
    rm -rf -- "$scratch" || code=1
    exit "$code"
}}
mkdir -m 700 "$scratch"
trap cleanup EXIT
trap 'exit 143' HUP INT TERM
mkdir "$scratch/checkout" "$scratch/data"
cp -a {source}/. "$scratch/checkout/"
rm -f "$scratch/checkout/.git"
git -C "$scratch/checkout" init -q
git -C {source} bundle create "$scratch/history.bundle" --all HEAD
git -C "$scratch/checkout" fetch -q "$scratch/history.bundle" '+refs/*:refs/*'
git -C "$scratch/checkout" fetch -q "$scratch/history.bundle" HEAD
git -C "$scratch/checkout" reset --mixed FETCH_HEAD
toolchain=$(rustc --print sysroot)
{setup if postgres else ''}
{invocation}
'''

RUST = {
    "rust": """cd rust
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo check --target x86_64-pc-windows-gnu -p pg-worker-core -p pg-spawnd --all-targets
cargo build -p pg-jail
PG_JAIL_BIN="$CARGO_TARGET_DIR/debug/pg-jail" cargo test""",
    "rust-worker": """cd rust/worker
command -v temporal >/dev/null 2>&1 || { echo 'rust-worker: host fault: Temporal CLI missing' >&2; exit 1; }
ports=$(python3 -c 'import socket
sockets = [socket.socket() for _ in range(3)]
for sock in sockets: sock.bind(("127.0.0.1", 0))
print(*(sock.getsockname()[1] for sock in sockets))')
set -- $ports
port=$1
temporal server start-dev --headless --ip 127.0.0.1 --port "$port" --http-port "$2" --metrics-port "$3" > "$scratch/temporal.log" 2>&1 &
server=$!
ready=0
for i in $(seq 1 30); do
    kill -0 "$server" 2>/dev/null || exit 1
    if temporal operator cluster health --address "127.0.0.1:$port" >/dev/null 2>&1; then ready=1; break; fi
    sleep 1
done
test "$ready" = 1
cargo fmt --check
cargo clippy --all-targets -- -D warnings
PG_TEMPORAL_ADDR=http://127.0.0.1:$port PG_JAIL=off PG_TEST_ALLOW_UNJAILED=1 cargo test""",
    "rust-spike": "cargo test --manifest-path rust/spikes/temporal-spike/Cargo.toml --test replay",
}

POSTGRES = """password=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export POSTGRES_PASSWORD="$password"
container_id=$(docker run -d --name "$container" -e POSTGRES_PASSWORD -e POSTGRES_DB=keeper -p 127.0.0.1:0:5432 postgres:16)
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
    body = '\n'.join(
        f"printf '%s\\n' {shlex.quote('Running: ' + line)} >&2\n{line}"
        if line.startswith(('cargo ', 'PG_JAIL_BIN=', 'PG_TEMPORAL_ADDR=', 'test "$ready"'))
        else line for line in body.splitlines())
    if name.startswith("rust"):
        body = ('command -v protoc >/dev/null 2>&1 || { '
                f"echo '{name}: host fault: protoc missing' >&2; exit 1; }}\n" + body)
    return f"""set -eu
set +x
scratch={scratch}
run=${{scratch%/*}}
container=dell-ci-${{run##*/}}-${{scratch##*/}}
container_id=
server=
cleanup() {{
    code=$?
    trap - EXIT HUP INT TERM
    if [ -n "$server" ]; then kill "$server" 2>/dev/null || :; wait "$server" 2>/dev/null || :; fi
    if [ -n "$container_id" ]; then
        docker rm -f "$container_id" >/dev/null 2>&1 || code=1
    fi
    rm -rf -- "$scratch" || code=1
    exit "$code"
}}
mkdir -m 700 "$scratch"
trap cleanup EXIT
trap 'exit 143' HUP INT TERM
export CARGO_TARGET_DIR="$scratch/target" CARGO_BUILD_JOBS=4
{body}
"""
