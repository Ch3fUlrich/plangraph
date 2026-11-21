import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time

import pytest

from _spawnd import built_rust_binary
from keeper_util import World, seed
from plangraph.keeper import api, auth, cli, config
from plangraph.keeper.evidence_git import GitAdapter

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def host(tmp_path, monkeypatch):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    log = tmp_path / 'commands'
    monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ['PATH'])
    for key in ('HOME', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'PLANGRAPH_SPAWN_STATE'):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setenv('COMMAND_LOG', str(log))
    monkeypatch.setenv('REAL_PYTHON', sys.executable)
    monkeypatch.setenv('PYTHONPATH', str(ROOT / 'src'))
    monkeypatch.setenv('AUTOOS_ROOT', str(tmp_path / 'autoos'))
    (tmp_path / 'autoos').mkdir()
    (tmp_path / 'autoos/setup.sh').write_text('echo setup >> "$COMMAND_LOG"\n')
    program = '''import os, subprocess, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['COMMAND_LOG'], 'a') as f:
    f.write(name + ' ' + ('-c' if args[:1] == ['-c'] else ' '.join(args)) + '\\n')
if 'tick' in args:
    print('silent: 0')
    sys.exit(int(os.environ.get('COUNT_EXIT', '0')))
if 'init-and-seed' in args:
    sys.exit(int(os.environ.get('CATALOG_EXIT', '0')))
if 'install' in args and os.environ.get('REAL_INSTALL'):
    sys.exit(subprocess.call([os.environ['REAL_PYTHON']] + args))
'''
    for name in ('python', 'python3', 'systemctl', 'ssh', 'schtasks', 'powershell.exe', 'AutoOS'):
        path = bin_dir / name
        path.write_text('#!' + sys.executable + '\n' + program)
        path.chmod(0o755)
    def run(*args, ps=False):
        cmd = [shutil.which('pwsh'), '-NoProfile', '-File', str(ROOT / 'deploy/bootstrap.ps1')] if ps else ['bash', str(ROOT / 'deploy/bootstrap.sh')]
        result = subprocess.run(cmd + list(args), capture_output=True, text=True, cwd=ROOT)
        return result, log.read_text().splitlines()
    return run, tmp_path


def test_bootstrap_dry_run_writes_nothing(host):
    run, root = host
    before = set(root.rglob('*'))
    result, commands = run('--dry-run', '--autoos-root', str(root / 'autoos'))
    assert result.returncode == 0, result.stderr
    assert sum('tick --dry-run' in c for c in commands) == 1
    assert all(c.endswith(' -c') or 'tick --dry-run' in c for c in commands)
    assert set(root.rglob('*')) - before == {root / 'commands'}


def test_language_servers_only_dry_run_uses_pins():
    result = subprocess.run(
        ['bash', str(ROOT / 'deploy/bootstrap.sh'), '--language-servers-only', '--dry-run'],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert 'rustup component add rust-analyzer --toolchain 1.100.0' in result.stdout
    assert 'pyright@1.1.414' in result.stdout
    assert 'Initializing Plangraph' not in result.stdout


def test_count_precedes_install(host):
    result, commands = host[0]('--skip-ctx-watchdog')
    assert result.returncode == 0, result.stderr
    assert next(i for i, c in enumerate(commands) if 'tick --dry-run' in c) < next(i for i, c in enumerate(commands) if 'init-and-seed' in c) < next(i for i, c in enumerate(commands) if 'install --host linux' in c)


@pytest.mark.parametrize('failure', ['COUNT_EXIT', 'CATALOG_EXIT'])
def test_count_failure_blocks_install(host, monkeypatch, failure):
    monkeypatch.setenv(failure, '7')
    result, commands = host[0]()
    assert result.returncode != 0
    assert not any('install' in c for c in commands)
    if failure == 'COUNT_EXIT':
        assert not any('init-and-seed' in c for c in commands)


def test_default_path_never_calls_autoos(host):
    result, commands = host[0]('--skip-ctx-watchdog')
    assert result.returncode == 0
    assert not any('AutoOS' in c or 'setup' in c for c in commands)
    assert 'AutoOS' not in result.stderr


def test_fake_systemctl_enables_watchdog_only(host, monkeypatch):
    monkeypatch.setenv('REAL_INSTALL', '1')
    result, commands = host[0]('--skip-ctx-watchdog')
    assert result.returncode == 0, result.stderr
    assert [c for c in commands if ' enable ' in c] == ['systemctl --user enable --now plangraph-stall-watchdog.timer']
    assert 'systemctl --user disable --now plangraph-remote-probe.timer' in commands


@pytest.mark.skipif(not shutil.which('pwsh'), reason='pwsh unavailable')
def test_powershell_same_order_and_failure(host, monkeypatch):
    result, commands = host[0]('-DryRun', ps=True)
    assert result.returncode == 0, result.stderr
    assert commands == ['python3 -m plangraph.spawner.stall_watchdog tick --dry-run']
    (host[1] / 'commands').unlink()
    result, commands = host[0](ps=True)
    assert result.returncode == 0, result.stderr
    assert ['tick' in commands[0], 'init-and-seed' in commands[1], 'install --host windows' in commands[2]] == [True] * 3
    (host[1] / 'commands').unlink()
    monkeypatch.setenv('COUNT_EXIT', '7')
    result, commands = host[0](ps=True)
    assert result.returncode != 0
    assert not any('install' in c or 'init-and-seed' in c for c in commands)


@pytest.fixture(scope='session')
def sweep_binary(tmp_path_factory):
    from _split_ci_environment import cargo_fixture_deadline

    cargo = shutil.which('cargo')
    if cargo is None:
        fallback = Path.home() / '.cargo/bin/cargo'
        if fallback.is_file():
            cargo = str(fallback)
    if cargo is None:
        pytest.fail('cargo is required to build pg-worktree-sweep')
    cargo_env = dict(os.environ)
    cargo_bin = str(Path(cargo).parent)
    if cargo_bin not in cargo_env.get('PATH', '').split(os.pathsep):
        cargo_env['PATH'] = cargo_bin + os.pathsep + cargo_env.get('PATH', '')
    shared = tmp_path_factory.getbasetemp()
    if shared.name.startswith('popen-gw'):
        shared = shared.parent
    with cargo_fixture_deadline(600), (shared / 'worktree-sweep-build.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        binary = built_rust_binary('pg-worktree-sweep', cargo_cwd=ROOT / 'rust')
        inputs = [
            *(ROOT / 'rust').rglob('*.rs'),
            *(ROOT / 'rust').rglob('Cargo.toml'),
            ROOT / 'rust/Cargo.lock',
        ]
        if not binary.exists() or any(
            p.stat().st_mtime > binary.stat().st_mtime
            for p in inputs
            if 'target' not in p.parts
        ):
            subprocess.run(
                [
                    'nice', '-n', '10', 'env', 'CARGO_BUILD_JOBS=4',
                    cargo, 'build', '-p', 'pg-worker-core', '--bin', 'pg-worktree-sweep',
                ],
                cwd=ROOT / 'rust',
                env=cargo_env,
                check=True,
            )
    return binary


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [
            'git',
            '-c', 'user.name=Fixture',
            '-c', 'user.email=test@example.invalid',
            '-c', 'core.hooksPath=/dev/null',
            '-c', 'commit.gpgsign=false',
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _parse_env_file(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, val = line.split('=', 1)
        parts = shlex.split(val.strip())
        values[key.strip()] = parts[0] if len(parts) == 1 else val.strip()
    return values


def _install_sweep_stubs(bin_dir: Path, merged_sha: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    systemctl = bin_dir / 'systemctl'
    systemctl.write_text(f'#!{sys.executable}\nimport sys\nsys.exit(0)\n', encoding='utf-8')
    systemctl.chmod(0o755)
    gh = bin_dir / 'gh'
    gh.write_text(
        f'#!{sys.executable}\n'
        'import json, sys\n'
        'args = sys.argv[1:]\n'
        "head = args[args.index('--head') + 1] if '--head' in args else ''\n"
        f'sha = {merged_sha!r}\n'
        "if head == 'merged-topic':\n"
        "    print(json.dumps([{'number': 7, 'state': 'MERGED', 'headRefOid': sha}]))\n"
        'else:\n'
        "    print('[]')\n",
        encoding='utf-8',
    )
    gh.chmod(0o755)
    cwds = bin_dir / 'pg-process-cwds'
    cwds.write_text(f"#!{sys.executable}\nprint('[[], false]')\n", encoding='utf-8')
    cwds.chmod(0o755)


def test_bootstrap_only_creates_sweep_config_when_token_exists(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    bins = tmp_path / 'bin'
    _install_sweep_stubs(bins, '0' * 40)
    env = dict(
        os.environ,
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / '.config'),
        XDG_STATE_HOME=str(home / '.local/state'),
        PLANGRAPH_SPAWN_STATE=str(home / '.local/state/plangraph'),
        PYTHONPATH=str(ROOT / 'src'),
        PATH=str(bins) + os.pathsep + os.environ['PATH'],
    )
    env.pop('PG_SWEEP_TOKEN_FILE', None)

    cmd = ['bash', str(ROOT / 'deploy/bootstrap.sh'), '--skip-autoos', '--skip-ctx-watchdog']
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
    assert res.returncode == 0
    assert not (home / '.config/plangraph/disk-hygiene.env').exists()
    assert not (home / '.config/plangraph/pins.json').exists()

    token = home / '.config/plangraph/sweep.token'
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text('placeholder-fixture\n', encoding='utf-8')
    token.chmod(0o600)

    dry = subprocess.run([*cmd, '--dry-run'], env=env, capture_output=True, text=True, check=True)
    assert '[DRY-RUN] worktree-sweep:' in dry.stdout
    assert not (home / '.config/plangraph/disk-hygiene.env').exists()
    assert not (home / '.config/plangraph/pins.json').exists()


def test_bootstrap_refuses_symlinked_sweep_token(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    bins = tmp_path / 'bin'
    _install_sweep_stubs(bins, '0' * 40)
    real_token = tmp_path / 'real.token'
    real_token.write_text('placeholder-fixture\n', encoding='utf-8')
    real_token.chmod(0o600)
    cfg_dir = home / '.config/plangraph'
    cfg_dir.mkdir(parents=True)
    token_link = cfg_dir / 'sweep.token'
    token_link.symlink_to(real_token)

    env = dict(
        os.environ,
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / '.config'),
        XDG_STATE_HOME=str(home / '.local/state'),
        PLANGRAPH_SPAWN_STATE=str(home / '.local/state/plangraph'),
        PYTHONPATH=str(ROOT / 'src'),
        PATH=str(bins) + os.pathsep + os.environ['PATH'],
    )
    env.pop('PG_SWEEP_TOKEN_FILE', None)

    cmd = ['bash', str(ROOT / 'deploy/bootstrap.sh'), '--skip-autoos', '--skip-ctx-watchdog']
    dry = subprocess.run([*cmd, '--dry-run'], env=env, capture_output=True, text=True, check=True)
    assert '[DRY-RUN] worktree-sweep:' not in dry.stdout
    res = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
    assert res.returncode == 0
    assert not (cfg_dir / 'disk-hygiene.env').exists()
    assert not (cfg_dir / 'pins.json').exists()

    # Keeper CLI helper also refuses a symlinked token file directly.
    assert cli.main([
        'admin', 'install-sweep',
        '--example', str(ROOT / 'deploy/systemd/disk-hygiene.env.example'),
        '--config-dir', str(cfg_dir),
        '--state-dir', str(home / '.local/state/plangraph'),
        '--token-file', str(token_link),
        '--env-file', str(cfg_dir / 'disk-hygiene.env'),
        '--pins-file', str(cfg_dir / 'pins.json'),
    ]) == 2
    assert not (cfg_dir / 'disk-hygiene.env').exists()
    assert not (cfg_dir / 'pins.json').exists()


def test_bootstrap_creates_sweep_env_and_pins_and_dry_run_lists_merged_worktree(tmp_path, sweep_binary):
    home = tmp_path / 'home'
    repo = home / 'code/plangraph'
    trees = home / 'code/plangraph-worktrees'
    claude_trees = repo / '.claude/worktrees'
    runs_dir = home / '.local/state/plangraph/runs'
    run_trees = home / '.local/state/plangraph/worktrees'
    for path in (repo, trees, claude_trees, runs_dir, run_trees):
        path.mkdir(parents=True, exist_ok=True)

    _git(repo, 'init', '-qb', 'main')
    (repo / 'README.md').write_text('init\n', encoding='utf-8')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'initial')
    remote = tmp_path / 'origin.git'
    remote.mkdir()
    _git(remote, 'init', '--bare', '-q')
    _git(repo, 'remote', 'add', 'origin', str(remote))
    _git(repo, 'push', '-q', 'origin', 'main')
    main_sha = _git(repo, 'rev-parse', 'HEAD')

    merged_wt = trees / 'merged-topic'
    _git(repo, 'worktree', 'add', '-qb', 'merged-topic', str(merged_wt))
    _git(repo, 'push', '-q', 'origin', 'merged-topic')
    old_ts = time.time() - 7200
    git_dir = Path(_git(merged_wt, 'rev-parse', '--absolute-git-dir'))
    for name in ('HEAD', 'index'):
        os.utime(git_dir / name, (old_ts, old_ts))

    claimed_wt = trees / 'active-topic'
    _git(repo, 'worktree', 'add', '-qb', 'lane/claimed', str(claimed_wt))
    _git(claimed_wt, 'commit', '--allow-empty', '-qm', 'wip')

    world = World(tmp_path)
    seed(world, [('CLAIMED', [])])
    world.store.ctx = {'git': GitAdapter({'demo': {'path': str(repo), 'ref': 'main'}})}
    world.store.claim(world.p('l1main'), 'demo:CLAIMED')
    spawner_tok = auth.add_principal(world.conn, 'install-spawner', 'spawner', [])
    world.store.record_run(
        auth.authenticate(world.conn, spawner_tok),
        'demo:CLAIMED',
        {'run_id': 'install-run-1', 'phase': 'started'},
    )
    run_rec = runs_dir / 'install-run-1/record.json'
    run_rec.parent.mkdir(parents=True, exist_ok=True)
    run_rec.write_text(json.dumps({'status': 'running', 'checkout': str(claimed_wt)}), encoding='utf-8')

    sweep_tok = auth.add_principal(world.conn, 'install-sweep', 'sweep', [])
    cfg_path = tmp_path / 'keeper.json'
    cfg_path.write_text(
        json.dumps({'db': str(world.path), 'snapshot': str(tmp_path / 'snap.json'), 'bind': '127.0.0.1:0'}),
        encoding='utf-8',
    )
    cfg_path.chmod(0o600)
    server = api.make_server(world.store, config.load_config(cfg_path), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        keeper_url = f'http://127.0.0.1:{server.server_address[1]}'
        cfg_dir = home / '.config/plangraph'
        cfg_dir.mkdir(parents=True, exist_ok=True)
        token_file = cfg_dir / 'sweep.token'
        token_file.write_text(sweep_tok + '\n', encoding='utf-8')
        token_file.chmod(0o600)
        (cfg_dir / 'plangraph.env').write_text(f'PG_URL={keeper_url}\n', encoding='utf-8')

        bins = tmp_path / 'bin'
        _install_sweep_stubs(bins, main_sha)
        boot_env = dict(
            os.environ,
            HOME=str(home),
            XDG_CONFIG_HOME=str(home / '.config'),
            XDG_STATE_HOME=str(home / '.local/state'),
            PLANGRAPH_SPAWN_STATE=str(home / '.local/state/plangraph'),
            PYTHONPATH=str(ROOT / 'src'),
            PATH=str(bins) + os.pathsep + os.environ['PATH'],
        )
        boot_env.pop('PG_URL', None)
        boot_env.pop('PG_TOKEN_FILE', None)
        boot_env.pop('PG_SWEEP_TOKEN_FILE', None)

        cmd = ['bash', str(ROOT / 'deploy/bootstrap.sh'), '--skip-autoos', '--skip-ctx-watchdog']
        subprocess.run(cmd, env=boot_env, check=True, capture_output=True, text=True)

        env_file = cfg_dir / 'disk-hygiene.env'
        pins_file = cfg_dir / 'pins.json'
        assert env_file.is_file()
        assert pins_file.is_file()
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
        assert json.loads(pins_file.read_text(encoding='utf-8')) == []

        parsed = _parse_env_file(env_file)
        assert parsed['PG_URL'] == keeper_url
        assert parsed['PG_TOKEN_FILE'] == str(token_file)
        assert parsed['PG_PINS'] == str(pins_file)
        assert 'plangraph.keeper.cli sweep-claims' in parsed['PG_SWEEP_CLAIMS_READER']

        # Re-running bootstrap preserves existing disk-hygiene.env and pins.json.
        env_file.write_text(env_file.read_text(encoding='utf-8') + '# preserved\n', encoding='utf-8')
        subprocess.run(cmd, env=boot_env, check=True, capture_output=True, text=True)
        assert '# preserved\n' in env_file.read_text(encoding='utf-8')

        sweep_env = dict(boot_env, **parsed, PG_PROCESS_CWD_READER=str(bins / 'pg-process-cwds'))
        sweep_res = subprocess.run(
            [
                str(sweep_binary),
                '--repo', parsed['PG_REPO'],
                '--root', parsed['PG_WORKTREES'],
                '--root', parsed['PG_CLAUDE_WORKTREES'],
                '--root', parsed['PG_RUNS'],
                '--root', parsed['PG_RUN_WORKTREES'],
                '--pins', parsed['PG_PINS'],
                '--state', str(home / '.local/state/plangraph'),
            ],
            env=sweep_env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert 'claims reader unavailable' not in sweep_res.stdout + sweep_res.stderr
        assert f'would-remove {merged_wt.resolve()}' in sweep_res.stdout
        assert f'protected {claimed_wt.resolve()}' in sweep_res.stdout
        assert merged_wt.exists() and claimed_wt.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        world.close()
