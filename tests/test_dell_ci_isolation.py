"""Ownership and output disclosure regression probes for Dell CI."""
import json
import subprocess
import os
from pathlib import Path
import shutil
import shlex
import socket
import uuid
import time

from test_dell_ci_run import Fake, ci, invoke
from dell_ci_jobs import sandbox_argv, isolated_script
import pytest


@pytest.mark.parametrize("failure", ["exit", "timeout", "oserror"])
def test_remote_output_never_persisted(tmp_path, failure):
    fake = Fake()
    canary = "remote-output-canary"

    def runner(argv, **kwargs):
        result = fake(argv, **kwargs)
        if argv[-1].startswith("timeout "):
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, 1, canary.encode(), canary)
            if failure == "oserror":
                raise OSError(canary)
            return subprocess.CompletedProcess(argv, 7, canary, canary)
        return result

    code, record = invoke(tmp_path, runner, jobs="pytest-postgres")
    assert code == 1
    assert canary not in json.dumps(record)
    assert "output_tail" not in record["jobs"][0]

def test_concurrent_container_ownership(tmp_path, monkeypatch):
    namespace = ci.service_script.__globals__
    monkeypatch.setitem(namespace, "POSTGRES", 'container_id="$container"\nsleep 0.1')
    docker = tmp_path / "docker"
    calls = tmp_path / "calls"
    docker.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\n')
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + ":/usr/bin:/bin")
    processes = []
    for run in ["run-one", "run-two"]:
        root = tmp_path / run
        root.mkdir()
        script = ci.service_script("pytest-postgres", str(root / "pytest-postgres"))
        processes.append(subprocess.Popen(["sh", "-c", script]))
    assert [process.wait(timeout=10) for process in processes] == [0, 0]
    assert set(calls.read_text().splitlines()) == {
        "rm -f dell-ci-run-one-pytest-postgres",
        "rm -f dell-ci-run-two-pytest-postgres"}

def test_failed_container_creation_does_not_remove_other_run(tmp_path, monkeypatch):
    namespace = ci.service_script.__globals__
    monkeypatch.setitem(namespace, "POSTGRES", "container_id=$(false)")
    script = ci.service_script("pytest-postgres", str(tmp_path / "scratch"))
    calls = tmp_path / "calls"
    docker_stub = ('calls_log=$1; docker() { '
                   'printf "%s\\n" "$*" >> "$calls_log"; exit 99; }; ')
    result = subprocess.run(
        ["sh", "-c", docker_stub + script, "sh", str(calls)])
    assert result.returncode == 1
    assert not calls.exists() or not any(
        line.startswith("rm -f ") for line in calls.read_text().splitlines())

def test_temporal_ports_and_liveness():
    body = ci.service_script.__globals__["RUST"]["rust-worker"]
    assert 'sock.bind(("127.0.0.1", 0))' in body
    assert "range(3)" in body
    assert '--port "$port" --http-port "$2" --metrics-port "$3"' in body
    assert 'kill -0 "$server" 2>/dev/null || exit 1' in body
    assert 'health --address "127.0.0.1:$port"' in body
    assert "PG_TEMPORAL_ADDR=http://127.0.0.1:$port" in body

def test_postgres_password_and_private_socket():
    script = isolated_script('true', '/driver', '/scratch/job', postgres=True)
    for expected in ('secrets.token_hex(32)', '-e POSTGRES_PASSWORD',
                     ' sh "$PG_DSN" "$password"', 'PGPASSWORD="$2"',
                     'PG_DSN="postgresql://postgres@/keeper?host=/scratch/pgsocket"',
                     '-v "$scratch/data/pgsocket:/job-socket"', 'host=/scratch/pgsocket',
                     'mkdir -m 700 "$scratch/data/pgsocket"', '--user "$(id -u):$(id -g)"'):
        assert expected in script
    for forbidden in ('postgres:$password@', '-p 127.0.0.1', 'trust'):
        assert forbidden not in script
    for option in ('POSTGRES_INITDB_ARGS="--auth-local=', 'POSTGRES_HOST_AUTH_METHOD="'):
        assert option + 'scram-sha-256"' in script
    argv = sandbox_argv('/checkout', '/scratch', '/rust', '/cargo', 'true')
    for forbidden in ('POSTGRES_PASSWORD', 'PG_DSN'):
        assert forbidden not in argv

def test_existing_scratch_is_not_removed(tmp_path):
    scratch = tmp_path / "owned-by-other-run"
    scratch.mkdir()
    result = subprocess.run(["sh", "-c", ci.service_script("rust-spike", str(scratch))])
    assert result.returncode != 0
    assert scratch.is_dir()

def test_postgres_template_marker_required(monkeypatch):
    monkeypatch.setitem(isolated_script.__globals__, 'POSTGRES', 'docker run postgres:16')
    with pytest.raises(ValueError, match='missing required marker'):
        isolated_script('true', '/driver', '/job', postgres=True)


@pytest.mark.parametrize('command', ['true && temporal', "true && 'temporal'", '"temporal"'])
def test_temporal_final_and_quoted_tokens(command):
    script = isolated_script(command, '/driver', '/job')
    assert 'host fault: Temporal CLI missing' in script
    assert '"$temporal" /toolchain/temporal-bin/temporal' in script


@pytest.mark.parametrize('available', [True, False])
def test_socket_readiness_blocks_invocation(tmp_path, available):
    script = isolated_script('true', '/driver', '/job', postgres=True)
    setup = script[script.index('mkdir -m 700 "$scratch/data/pgsocket"'):]
    setup = setup[:setup.index('/usr/bin/bwrap')]
    scratch = tmp_path / 'job'
    (scratch / 'data').mkdir(parents=True)
    # Simulate delayed startup without a wall-clock sleep. Invocation must not
    # proceed until the third successful readiness attempt (or after timeout).
    prelude = f'''scratch={shlex.quote(str(scratch))}
container=probe
docker() {{
    if [ "$1" = run ]; then echo probe; return; fi
    case "$*" in *'pg_isready -h /job-socket'*) ;; *) return 99;; esac
    n=$(cat "$scratch/attempts" 2>/dev/null || echo 0)
    n=$((n+1)); echo "$n" > "$scratch/attempts"
    [ "$n" -ge 3 ] && {str(available).lower()}
}}
sleep() {{ :; }}
'''
    result = subprocess.run(['sh', '-ec', prelude + setup + '\necho invoked'],
                            capture_output=True, text=True, timeout=10)
    assert int((scratch / 'attempts').read_text()) == (3 if available else 50)
    assert (result.returncode == 0) == available
    assert ('invoked' in result.stdout) == available
    if not available:
        assert 'socket readiness timed out' in result.stderr

def test_isolated_script_quotes_paths_and_preserves_command(tmp_path, monkeypatch):
    source = tmp_path / 'source space $(touch injected)'
    job = tmp_path / 'job space $(touch injected)'
    command = 'git diff SCRATCH CHECKOUT TOOLCHAIN CARGOBIN'
    script = isolated_script(command, source, job)
    assignment = next(line for line in script.splitlines() if line.startswith('scratch='))
    result = subprocess.run(['sh', '-c', assignment + '\nprintf "%s" "$scratch"'],
                            capture_output=True, text=True, check=True)
    assert result.stdout == str(job)
    copy = next(line for line in script.splitlines() if line.startswith('cp -a '))
    bundle = next(line for line in script.splitlines() if line.startswith('git -C ') and 'bundle create' in line)
    assert shlex.split(copy)[2] == str(source) + '/.'
    assert shlex.split(bundle)[2] == str(source)
    invocation = script.splitlines()[-1]
    # Evaluate the generated shell quoting without launching a sandbox.
    capture = 'capture() { python3 -c \'import json,sys; print(json.dumps(sys.argv[1:]))\' "$@"; }; '
    monkeypatch.setenv('scratch', str(job))
    monkeypatch.setenv('toolchain', str(tmp_path / 'rust space'))
    monkeypatch.setenv('cargo_bin', '/usr/bin')
    result = subprocess.run(['sh', '-c', capture + invocation.replace('/usr/bin/bwrap', 'capture', 1)],
                            capture_output=True, text=True, check=True)
    argv = json.loads(result.stdout)
    assert argv[-1] == command
    assert str(job / 'checkout') in argv
    assert str(job / 'data') in argv
    assert str(tmp_path / 'rust space') in argv
    assert not (tmp_path / 'injected').exists()

def test_sandbox_capabilities(tmp_path):
    commands = [sandbox_argv(str(tmp_path / job / "checkout"),
                            str(tmp_path / job / "scratch"),
                            "/opt/rust", "/opt/cargo/bin", "true")
                for job in ("one", "two")]
    for argv in commands:
        assert "--clearenv" in argv
        assert all(flag in argv for flag in ("--unshare-user", "--unshare-pid",
                                            "--unshare-ipc", "--unshare-net", "--proc"))
        sources = [argv[i + 1] for i, value in enumerate(argv)
                   if value in {"--bind", "--ro-bind"}]
        for forbidden in ("/var/run", "/run/docker.sock", str(Path.home() / ".ssh"),
                          str(Path.home() / ".cache"), str(Path.home() / ".local/bin"),
                          str(Path(__file__).resolve().parents[1])):
            for source in sources:
                assert not Path(forbidden).resolve().is_relative_to(
                    Path(source).expanduser().resolve()), (source, forbidden)
                assert not Path(source).expanduser().resolve().is_relative_to(
                    Path(forbidden).resolve()), (source, forbidden)
        keys = [argv[i + 1] for i, value in enumerate(argv) if value == "--setenv"]
        assert set(keys) == {"HOME", "PATH", "CARGO_HOME", "CARGO_TARGET_DIR",
                             "RUSTUP_HOME", "XDG_CACHE_HOME", "PYTHONPATH",
                              "CARGO_BUILD_JOBS", "CARGO_NET_OFFLINE", "LANG"}
        assert ".local/bin" not in " ".join(argv)
    assert commands[0] != commands[1]
    assert set(commands[0]) & {str(tmp_path / "two/scratch")} == set()

def test_every_job_uses_fresh_sandbox(tmp_path):
    fake = Fake()
    code, _ = invoke(tmp_path, fake, jobs=",".join(ci.JOBS))
    assert code == 0
    commands = [c for c in fake.calls if c.startswith("timeout ")]
    assert len(commands) == len(ci.JOBS)
    for name, command in zip(ci.JOBS, commands):
        assert "/usr/bin/bwrap --unshare-user --unshare-pid" in command
        assert "--clearenv" in command
        assert f"/{name}" in command
        assert 'cp -a ' in command and '"$scratch/checkout/"' in command
        assert "bundle create" in command and "--all HEAD" in command
        assert "$HOME/.local/bin" not in command


@pytest.fixture
def real_sandbox(tmp_path):
    if not shutil.which("bwrap"):
        pytest.fail("bwrap unavailable: isolation probes must execute on the Dell host")
    checkout, scratch = tmp_path / "checkout", tmp_path / "scratch"
    checkout.mkdir()
    scratch.mkdir()
    def run(command, **capabilities):
        return subprocess.run(sandbox_argv(str(checkout), str(scratch),
                                          subprocess.check_output(['rustc', '--print', 'sysroot'], text=True).strip(),
                                          "/usr/bin", command, **capabilities),
                              capture_output=True, text=True, timeout=20)
    probe = run("true")
    if probe.returncode:
        pytest.fail("bwrap probe failed: " + probe.stderr)
    run.argv = sandbox_argv(str(checkout), str(scratch), '/usr', '/usr/bin', 'true')
    run.scratch = scratch
    return run

def test_real_sandbox_requires_bwrap(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda _: None)
    with pytest.raises(pytest.fail.Exception, match='bwrap unavailable'):
        real_sandbox.__wrapped__(tmp_path)


@pytest.mark.parametrize("attack", ["other-job", "checkout", "ssh", "docker", "env", "proc"])
def test_real_sandbox_denies_host_access(tmp_path, real_sandbox, attack, monkeypatch, request):
    other = tmp_path / "other-job"
    other.mkdir()
    host = Path(__file__).resolve().parents[1]
    host_pid = next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines()
                    if line.startswith('NSpid:'))
    monkeypatch.setenv("DELL_CI_SECRET_CANARY", "host-secret")
    ssh = tmp_path / '.ssh'
    ssh.mkdir()
    (ssh / 'canary').write_text('private-key-canary')
    docker = socket.socket(socket.AF_UNIX)
    docker_path = tmp_path / 'docker.sock'
    docker.bind(str(docker_path))
    docker.listen()
    # Prove that the target exists and accepts host connections before probing.
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(docker_path))
    request.addfinalizer(docker.close)
    commands = {
        "other-job": f"touch {other}/poison",
        "checkout": f"touch {host}/sandbox-escape-probe",
        "ssh": f"cat {shlex.quote(str(ssh / 'canary'))}",
        "docker": 'python3 -c ' + shlex.quote(
            f'import socket; s=socket.socket(socket.AF_UNIX); s.connect({str(docker_path)!r})'),
        "env": 'test -n "$DELL_CI_SECRET_CANARY"',
        "proc": f"cat /proc/{host_pid}/environ || cat /proc/1/root/proc/{host_pid}/environ",
    }
    result = real_sandbox(commands[attack])
    assert result.returncode != 0, result.stdout
    if attack in {'ssh', 'docker'}:
        assert 'No such file or directory' in result.stderr, result.stderr
        assert 'bwrap:' not in result.stderr
        assert 'private-key-canary' not in result.stdout
    if attack == 'checkout':
        argv = real_sandbox.argv
        for i, flag in enumerate(argv):
            if flag in {'--bind', '--ro-bind'}:
                source = Path(argv[i + 1]).resolve()
                if host.is_relative_to(source):
                    destination = Path(argv[i + 2]) / host.relative_to(source)
                    assert real_sandbox(f'touch {shlex.quote(str(destination / "sandbox-escape-probe"))}').returncode != 0
    assert not (other / "poison").exists()
    assert not (host / "sandbox-escape-probe").exists()

def test_real_sandbox_protoc_and_cargo(real_sandbox, monkeypatch):
    protoc = shutil.which('protoc', path=os.environ.get('PATH', '') + os.pathsep + str(Path.home() / '.local/bin'))
    assert protoc, 'trusted driver requires protoc'
    monkeypatch.setitem(ci.service_script.__globals__['RUST'], 'rust-spike',
                        'command -v protoc && cargo --version && protoc --version')
    result = real_sandbox(ci.service_script('rust-spike', '/scratch/rust-probe'),
                          protoc=str(Path(protoc).resolve()))
    assert result.returncode == 0, result.stderr
    assert '/toolchain/protoc-bin/protoc' in result.stdout
    assert 'libprotoc ' in result.stdout
    assert 'cargo ' in result.stdout
    print(result.stdout)

def test_real_sandbox_browser_cache(real_sandbox):
    cache = Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH',
                                Path.home() / '.cache/ms-playwright')).resolve()
    assert cache.is_dir(), 'trusted driver requires installed Playwright browsers'
    result = real_sandbox('test -n "$(find "$PLAYWRIGHT_BROWSERS_PATH" -name chrome -type f)" '
                          '&& ! touch "$PLAYWRIGHT_BROWSERS_PATH/poison"', browsers=str(cache))
    assert result.returncode == 0, result.stderr

def test_real_sandbox_network_and_job_services(real_sandbox, tmp_path):
    temporal = shutil.which('temporal', path=os.environ.get('PATH', '') + os.pathsep + str(Path.home() / '.local/bin'))
    assert temporal, 'trusted Temporal CLI required (set PATH for the test)'
    temporal = str(Path(temporal).resolve())
    script = isolated_script('temporal --version', '/driver', '/job')
    assert '"$temporal" /toolchain/temporal-bin/temporal' in script
    assert 'command -v "${HOME}/.local/bin/temporal"' in script
    with socket.socket() as listener, socket.socket(socket.AF_UNIX) as abstract:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        address = '\0dell-ci-' + uuid.uuid4().hex
        abstract.bind(address)
        abstract.listen()
        probe = f'''import socket
for family, address in [(socket.AF_INET, ('127.0.0.1', {listener.getsockname()[1]})),
                        (socket.AF_UNIX, {address!r})]:
    with socket.socket(family) as s:
        s.settimeout(1)
        assert s.connect_ex(address) != 0, 'host service reachable'
print('host network denied')
'''
        pg_socket = real_sandbox.scratch / 'pgsocket'
        pg_socket.mkdir(mode=0o700)
        auth = 'scram-sha-256'
        container = subprocess.check_output([
            'docker', 'run', '-d', '--rm', '--user', f'{os.getuid()}:{os.getgid()}',
            '-e', 'PGDATA=/tmp/pgdata',
            '-e', 'POSTGRES_PASSWORD=probe-only',
            '-e', f'POSTGRES_HOST_AUTH_METHOD={auth}',
            '-e', f'POSTGRES_INITDB_ARGS=--auth-local={auth}',
            '--tmpfs', f'/var/run/postgresql:uid={os.getuid()},gid={os.getgid()},mode=0700',
            '-v', f'{pg_socket}:/job-socket', 'postgres:16',
            '-c', 'listen_addresses=', '-c', 'unix_socket_directories=/job-socket,/var/run/postgresql'], text=True).strip()
        pg_probe = r"""import socket, struct, time
for attempt in range(100):
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect('/scratch/pgsocket/.s.PGSQL.5432')
        break
    except OSError:
        s.close()
        time.sleep(.1)
else:
    raise AssertionError('job PostgreSQL socket unavailable')
payload = b'user\0postgres\0database\0postgres\0\0'
s.sendall(struct.pack('!II', len(payload)+8, 196608) + payload)
def message():
    kind = s.recv(1)
    assert kind, 'PostgreSQL disconnected'
    length = struct.unpack('!I', s.recv(4))[0] - 4
    data = b''
    while len(data) < length:
        data += s.recv(length-len(data))
    assert kind != b'E', data
    return kind, data
kind, data = message()
assert kind == b'R' and struct.unpack('!I', data[:4])[0] == 10, data
print('job PostgreSQL requires SCRAM')
raise SystemExit(0)
"""
        command = 'set -eu\npython3 -c ' + shlex.quote(probe) + '\n' + 'python3 -c ' + shlex.quote(pg_probe) + '''
command -v temporal
temporal --version
temporal server start-dev --headless --ip 127.0.0.1 --port 17233 --http-port 18233 --metrics-port 19233 >/scratch/temporal.log 2>&1 &
server=$!
trap 'kill "$server"' EXIT
ready=0
for i in $(seq 1 50); do
    if temporal operator cluster health --address 127.0.0.1:17233; then ready=1; break; fi
    sleep 0.2
done
test "$ready" = 1
'''
        def remove_container(*, check):
            return subprocess.run(['docker', 'rm', '-f', container], check=check,
                                  capture_output=True, timeout=10)
        try:
            for _ in range(500):
                # The entrypoint's temporary initialization server also reports
                # ready; wait until postgres replaces the entrypoint as PID 1.
                ready = subprocess.run(['docker', 'exec', container, 'sh', '-c',
                                        'test "$(cat /proc/1/comm)" = postgres && pg_isready -h /job-socket -U postgres'],
                                       capture_output=True, timeout=5)
                if ready.returncode == 0:
                    break
                time.sleep(.1)
            assert ready.returncode == 0, ready.stderr
            assert pg_socket.stat().st_uid == os.getuid()
            assert pg_socket.stat().st_mode & 0o777 == 0o700
            result = real_sandbox(command, temporal=temporal)
            # Force the --rm race: final cleanup must not mask probe results
            # when the container has already disappeared.
            remove_container(check=True)
        finally:
            remove_container(check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'host network denied' in result.stdout
        assert '/toolchain/temporal-bin/temporal' in result.stdout
