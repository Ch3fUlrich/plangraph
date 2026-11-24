"""The writer environment is scrubbed before test fixtures execute."""
import os
import subprocess
import sys
from pathlib import Path
import pytest

WRITER_VARIABLES = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_COMMITTER_NAME",
    "GIT_COMMITTER_EMAIL",
    "OPENCODE_DISABLE_PROJECT_CONFIG",
    "OPENCODE_DISABLE_CLAUDE_CODE",
    "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
)


@pytest.fixture(scope="module")
def seeded_writer_env():
    """Module scope sets up before the function-scoped autouse scrub in tests/conftest.py."""
    patch = pytest.MonkeyPatch()
    for name in WRITER_VARIABLES:
        patch.setenv(name, "writer-sentinel")
    yield
    patch.undo()


def test_writer_variables_are_removed_before_test(seeded_writer_env):
    for name in WRITER_VARIABLES:
        assert name not in os.environ, name


@pytest.mark.parametrize("child", ["ops", "bin"])
@pytest.mark.parametrize("damage", ["loop", "permission"])
def test_strict_routing_child_stat_errors(tmp_path, monkeypatch, child, damage):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    import autoos_refs_roots as roots

    target = tmp_path / child
    if damage == "loop":
        target.symlink_to(child)
    else:
        target.mkdir()
        if os.geteuid() == 0:
            pytest.skip("root can search mode 0444 directories")
        tmp_path.chmod(0o444)
    try:
        with pytest.raises(roots.RootScanError, match="cannot read"):
            roots._scan_routing_tree(tmp_path, True)
    finally:
        tmp_path.chmod(0o700)


def test_gateway_config_is_isolated_from_home(tmp_path, monkeypatch):
    from plangraph.routing_support.clients import effective_gateway_base_url
    from plangraph.spawner import gateway_witness as gw

    # A real-looking HOME has an endpoint, but the shared fixture's XDG root
    # must keep this file out of ordinary endpoint resolution.
    home = tmp_path / "home"
    config = home / ".config"
    planted = config / "plangraph" / "omniroute-base-url"
    planted.parent.mkdir(parents=True)
    endpoint = "https://planted.invalid/v1"
    planted.write_text(endpoint)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PLANGRAPH_GATEWAY_URL")
    isolated = Path(os.environ["XDG_CONFIG_HOME"])
    assert isolated != config
    reads = []
    open_config = os.open

    def record_read(path, *args, **kwargs):
        reads.append(Path(path))
        return open_config(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", record_read)
    assert effective_gateway_base_url() is None
    with pytest.raises(LookupError, match="gateway endpoint not configured"):
        gw.fetch_rows("plangraph-job", dict(os.environ), opener=pytest.fail)
    assert reads and set(reads) == {isolated / "plangraph" / "omniroute-base-url"}
    # Opt in to file configuration explicitly; also cover HOME/.config with
    # an injected environment so no real user's home can ever be consulted.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    assert effective_gateway_base_url() == endpoint
    assert effective_gateway_base_url({"HOME": str(home)}) == endpoint


def test_session_git_configuration_is_isolated():
    assert os.environ.get('GIT_CONFIG_NOSYSTEM') == '1'
    path = Path(os.environ['GIT_CONFIG_GLOBAL'])
    assert path.name == 'global.gitconfig' and path.is_file()


@pytest.mark.parametrize('worktree', [False, True])
def test_git_config_guard_detects_change_at_teardown(tmp_path, worktree, config_guard):
    repo = tmp_path / 'repo'
    repo.mkdir()
    if worktree:
        metadata = tmp_path / 'metadata'
        gitdir = metadata / 'worktrees/lane'
        gitdir.mkdir(parents=True)
        (repo / '.git').write_text('gitdir: ' + str(gitdir) + '\n')
        (gitdir / 'commondir').write_text('../..\n')
    else:
        metadata = repo / '.git'
        metadata.mkdir()
    config = metadata / 'config'
    config.write_text('[core]\n bare = false\n')
    with pytest.raises(AssertionError, match='repository .git/config changed during pytest session'):
        with config_guard(repo):
            config.write_text('[core]\n bare = true\n')


def test_pytest_shards_preserve_groups_and_cover_every_case(shard_selector):
    from types import SimpleNamespace
    def item(name, group=None):
        return SimpleNamespace(nodeid=name, get_closest_marker=lambda _: SimpleNamespace(args=(group,), kwargs={}) if group else None)
    items = [item('a', 'together'), item('b', 'together'), item('c'), item('d')]
    shards = [shard_selector(items, f'{n}/3') for n in range(3)]
    assert sorted(x.nodeid for shard in shards for x in shard) == ['a', 'b', 'c', 'd']
    assert sum(any(x.nodeid == 'a' for x in shard) and any(x.nodeid == 'b' for x in shard) for shard in shards) == 1
    assert [[x.nodeid for x in shard_selector(list(reversed(items)), f'{n}/3')][::-1] for n in range(3)] == [[x.nodeid for x in shard] for shard in shards]


@pytest.mark.parametrize('linked', [False, True])
@pytest.mark.parametrize('change', ['add', 'remove', 'relocate'])
def test_guard_detects_worktree_registry_changes(tmp_path, config_guard, linked, change):
    repo, common = tmp_path / 'repo', tmp_path / 'metadata'
    repo.mkdir()
    (common / 'worktrees/old').mkdir(parents=True)
    (common / 'config').write_text('[core]\n')
    entry = common / 'worktrees/old/gitdir'
    entry.write_text(str(tmp_path / 'old/tree/.git') + '\n')
    if linked:
        (repo / '.git').write_text('gitdir: ' + str(common / 'worktrees/old') + '\n')
        (common / 'worktrees/old/commondir').write_text('../..\n')
    else:
        (repo / '.git').symlink_to(common, target_is_directory=True)
    with pytest.raises(AssertionError, match='worktree registry changed'):
        with config_guard(repo, registry_roots=(tmp_path,)):
            if change == 'add':
                (common / 'worktrees/new').mkdir()
                (common / 'worktrees/new/gitdir').write_text(str(tmp_path / 'new/tree/.git') + '\n')
            elif change == 'remove':
                entry.unlink()
            else:
                entry.write_text(str(tmp_path / 'moved/tree/.git') + '\n')


def test_guard_ignores_other_sessions_worktree_moves(tmp_path, config_guard, monkeypatch):
    repo = tmp_path / 'repo'
    metadata = repo / '.git'
    other = metadata / 'worktrees/other'
    other.mkdir(parents=True)
    (metadata / 'config').write_text('[core]\n')
    (metadata / 'HEAD').write_text('ref: refs/heads/main\n')
    (other / 'gitdir').write_text(str(tmp_path / 'other/.git'))
    with config_guard(repo, registry_roots=(tmp_path / 'test-owned',)):
        (metadata / 'HEAD').write_text('ref: refs/heads/another-session\n')
        (other / 'HEAD').write_text('b' * 40)
        (other / 'gitdir').write_text(str(tmp_path / 'elsewhere/.git'))
        read_text = Path.read_text
        def disappearing(path, *args, **kwargs):
            if path == other / 'gitdir': path.unlink()
            return read_text(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', disappearing)


@pytest.mark.parametrize('boundary', ['preflight', 'selector', 'pytest'])
def test_test_environment_uses_git_discovery_and_removes_config_prefix(tmp_path, monkeypatch, boundary, request):
    import runpy
    from test_affected_tests import selector
    root = Path(__file__).resolve().parents[1]
    # Nested suites register their own conftest under the same Python module name.
    # Exercise that collision even when this file is selected without those suites.
    monkeypatch.setitem(sys.modules, 'conftest', object())
    root_conftest = request.config.pluginmanager.get_plugin(str(root / 'tests/conftest.py'))
    assert root_conftest is not None
    helper = (runpy.run_path(str(root / 'tools/preflight.py'))['test_environment'] if boundary == 'preflight'
              else selector().test_environment if boundary == 'selector' else root_conftest._test_environment)
    inherited = dict(GIT_DIR='caller', GIT_FUTURE_BINDING='caller', GIT_PREFIX='prefix/',
                     GIT_CONFIG='caller', GIT_CONFIG_GLOBAL='caller', GIT_CONFIG_NOSYSTEM='1',
                     GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_7='caller', GIT_CONFIG_VALUE_7='caller',
                     git_config_future='caller', PATH='preserved', GIT_SSH_COMMAND='preserved')
    before = dict(inherited)
    def discover(command, **kwargs):
        assert command == ['git', 'rev-parse', '--local-env-vars']
        assert kwargs['env'] == inherited
        return 'GIT_DIR\nGIT_FUTURE_BINDING\n'
    monkeypatch.setattr(subprocess, 'check_output', discover)
    assert helper(tmp_path, inherited) == {'PATH': 'preserved', 'GIT_SSH_COMMAND': 'preserved'}
    assert inherited == before  # Hook-owned Git calls retain their original context.


def test_selector_git_discovery_failure_never_launches_tests(tmp_path, monkeypatch):
    from test_affected_tests import selector
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_fixture.py').touch()
    def unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(subprocess, 'check_output', unavailable)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('test process launched'))
    module = selector()
    monkeypatch.setattr(module, 'select_tests', lambda *a, **k: {'tests': ['tests/test_fixture.py']})
    monkeypatch.setattr(sys, 'argv', ['affected_tests', '--base', 'HEAD', '--changed',
                                   'tests/test_fixture.py', '--run'])
    assert module.main(root=tmp_path) == 1


@pytest.mark.parametrize("filename", ["docker.sock", "anchor.sock", "silent.sock"])
def test_short_socket_dir_ignores_long_tmpdir(tmp_path, monkeypatch, request, filename):
    import socket
    import tempfile
    long_root = tmp_path / ("long-user-" * 12)
    long_root.mkdir()
    monkeypatch.setenv("TMPDIR", str(long_root))
    monkeypatch.setattr(tempfile, "tempdir", str(long_root))
    directory = request.getfixturevalue("short_socket_dir")
    address = directory / filename
    assert directory.parent == Path("/tmp")
    assert len(os.fsencode(address)) < 100
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(address))
        server.listen()
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(address))


@pytest.mark.parametrize("encoded", [False, True])
def test_unix_socket_guard_rejects_tmp_path(tmp_path, encoded):
    import socket
    address = tmp_path / "bad.sock"
    with socket.socket(socket.AF_UNIX) as server:
        with pytest.raises(AssertionError, match="use short_socket_dir"):
            server.bind(os.fsencode(address) if encoded else str(address))


@pytest.mark.parametrize("encoded", [False, True])
def test_unix_socket_guard_rejects_long_paths(encoded):
    import socket
    address = "/tmp/" + "é" * 48
    assert len(address) < 100 <= len(os.fsencode(address))
    with socket.socket(socket.AF_UNIX) as server:
        with pytest.raises(AssertionError, match="use short_socket_dir"):
            server.bind(os.fsencode(address) if encoded else address)


def test_pytest_shards_balance_recorded_durations(tmp_path, monkeypatch, shard_selector):
    import json
    from types import SimpleNamespace

    timings = {'a': 9, 'b': 6, 'c': 4, 'd': 3, 'e': 2}
    path = tmp_path / 'durations.json'
    path.write_text(json.dumps(timings))
    monkeypatch.setenv('PYTEST_SHARD_DURATIONS_FILE', str(path))
    items = [
        SimpleNamespace(
            nodeid=name,
            get_closest_marker=lambda _, name=name: (
                SimpleNamespace(args=('group',), kwargs={}) if name in ('c', 'e') else None
            ),
        )
        for name in 'abcde'
    ]
    shards = [shard_selector(items, f'{index}/2') for index in range(2)]
    assert [[item.nodeid for item in shard] for shard in shards] == [['a', 'd'], ['b', 'c', 'e']]
    assert [sum(timings[item.nodeid] for item in shard) for shard in shards] == [12, 12]


@pytest.mark.parametrize(('now', 'mode', 'expected'), [
    ('2026-11-24T09:00:00+00:00', 'blocking', ['test_regular']),
    ('2026-11-24T09:00:00+00:00', 'quarantine', ['test_flaky[user@example.invalid]']),
    ('2026-11-25T09:00:00+00:00', 'blocking', ['test_regular', 'test_flaky[user@example.invalid]']),
    ('2026-11-25T09:00:00+00:00', 'quarantine', []),
])
def test_pytest_quarantine_expires(tmp_path, now, mode, expected):
    import json
    from datetime import datetime
    from types import SimpleNamespace

    from _split_ci_environment import quarantine_items

    ledger = tmp_path / 'quarantine.json'
    ledger.write_text(json.dumps({
        'test_flaky[user@example.invalid]': {
            'owner': 'plangraph:FIX-THE-FLAKE',
            'starts': '2026-11-23T09:00:00+00:00',
            'expires': '2026-11-25T09:00:00+00:00',
        }
    }))
    items = [SimpleNamespace(nodeid=nodeid) for nodeid in ['test_regular', 'test_flaky[user@example.invalid]@contract']]
    selected, active = quarantine_items(items, ledger, mode, datetime.fromisoformat(now))
    assert [item.nodeid.removesuffix('@contract') for item in selected] == expected
    assert active == ({'test_flaky[user@example.invalid]': 'plangraph:FIX-THE-FLAKE'} if now.startswith('2026-11-24') else {})


def _quarantine_entry(**overrides):
    entry = {
        'owner': 'plangraph:FIX-THE-FLAKE',
        'starts': '2026-11-23T09:00:00+00:00',
        'expires': '2026-11-25T09:00:00+00:00',
    }
    entry.update(overrides)
    return entry


@pytest.mark.parametrize('entry', [
    _quarantine_entry(expires='2026-11-25T10:00:00+00:00'),  # 49 h window
    _quarantine_entry(expires='2026-11-23T09:00:00+00:00'),  # zero-length window
    _quarantine_entry(expires='2026-11-22T09:00:00+00:00'),  # negative window
    _quarantine_entry(owner='FIX-THE-FLAKE'),
    _quarantine_entry(owner='plangraph:'),
    _quarantine_entry(owner=None),
    {key: value for key, value in _quarantine_entry().items() if key != 'owner'},
    _quarantine_entry(starts='2026-11-23T09:00:00'),  # naive timestamp
    _quarantine_entry(expires='2026-11-25T09:00:00'),
    _quarantine_entry(starts='2026-11-23T10:00:00+01:00', expires='2026-11-25T10:00:00+01:00'),  # not UTC
], ids=['49h', 'zero', 'negative', 'bare-owner', 'empty-owner', 'null-owner', 'missing-owner',
        'naive-starts', 'naive-expires', 'non-utc'])
def test_pytest_quarantine_rejects_unsafe_ledger_entries(tmp_path, entry):
    import json
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from _split_ci_environment import quarantine_items

    ledger = tmp_path / 'quarantine.json'
    ledger.write_text(json.dumps({'test_flaky': entry}))
    with pytest.raises(pytest.UsageError, match='PYTEST_QUARANTINE_FILE'):
        quarantine_items([SimpleNamespace(nodeid='test_flaky')], ledger, 'blocking',
                         datetime(2026, 11, 24, 9, tzinfo=timezone.utc))


def test_pytest_quarantine_accepts_a_window_of_exactly_48_hours(tmp_path):
    import json
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from _split_ci_environment import quarantine_items

    ledger = tmp_path / 'quarantine.json'
    ledger.write_text(json.dumps({'test_flaky': _quarantine_entry()}))
    selected, active = quarantine_items([SimpleNamespace(nodeid='test_flaky')], ledger, 'quarantine',
                                        datetime(2026, 11, 24, 9, tzinfo=timezone.utc))
    assert [item.nodeid for item in selected] == ['test_flaky'] and active == {'test_flaky': 'plangraph:FIX-THE-FLAKE'}


@pytest.mark.parametrize(('mode', 'expected'), [('blocking', ['test_flaky']), ('quarantine', [])])
def test_pytest_quarantine_window_that_has_not_begun_stays_blocking(tmp_path, mode, expected):
    import json
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from _split_ci_environment import quarantine_items

    ledger = tmp_path / 'quarantine.json'
    ledger.write_text(json.dumps({'test_flaky': _quarantine_entry(
        starts='2026-11-25T09:00:00+00:00', expires='2026-11-26T09:00:00+00:00')}))
    selected, active = quarantine_items([SimpleNamespace(nodeid='test_flaky')], ledger, mode,
                                        datetime(2026, 11, 24, 9, tzinfo=timezone.utc))
    assert [item.nodeid for item in selected] == expected and active == {}


def test_pytest_quarantine_rejects_a_naive_run_timestamp(tmp_path):
    import json
    from datetime import datetime
    from types import SimpleNamespace

    from _split_ci_environment import quarantine_items

    ledger = tmp_path / 'quarantine.json'
    ledger.write_text(json.dumps({'test_flaky': _quarantine_entry()}))
    with pytest.raises(pytest.UsageError, match='PYTEST_QUARANTINE_FILE'):
        quarantine_items([SimpleNamespace(nodeid='test_flaky')], ledger, 'blocking', datetime(2026, 11, 24, 9))


@pytest.mark.parametrize('parallel', [False, True])
def test_pytest_writes_shard_durations_through_conftest(tmp_path, parallel):
    import json
    import subprocess

    root = Path(__file__).resolve().parents[1]
    nodeid = 'tests/test_conftest_env.py::test_pytest_shards_preserve_groups_and_cover_every_case'
    data = tmp_path / 'durations.json'
    worker_args = []
    if parallel:
        pytest.importorskip('xdist')
        worker_args = ['-n', '2', '--dist', 'loadgroup']
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', *worker_args, nodeid],
        cwd=root, env=dict(_nested_pytest_env(), PYTEST_SHARD_DURATIONS_OUT=str(data)),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    durations = json.loads(data.read_text(encoding='utf-8'))
    assert list(durations) == [nodeid]
    assert durations[nodeid] > 0


def test_pytest_shard_durations_sum_setup_call_and_teardown(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from _pytest.reports import TestReport
    from _split_ci_environment import pytest_terminal_summary

    def report(nodeid, when, duration):
        return TestReport(nodeid, ('f.py', 0, 't'), {}, 'passed', None, when, duration=duration)

    stats = {'passed': [report('t.py::a@grp', 'setup', 0.5), report('t.py::a@grp', 'call', 2.0),
                        report('t.py::a@grp', 'teardown', 0.25)]}
    out = tmp_path / 'durations.json'
    monkeypatch.setenv('PYTEST_SHARD_DURATIONS_OUT', str(out))
    pytest_terminal_summary(SimpleNamespace(stats=stats), pytest.ExitCode.OK)
    assert json.loads(out.read_text(encoding='utf-8')) == {'t.py::a': 2.75}


def test_pytest_quarantine_is_applied_during_collection(tmp_path):
    import json
    import subprocess

    root = Path(__file__).resolve().parents[1]
    nodeid = 'tests/test_conftest_env.py::test_pytest_shards_preserve_groups_and_cover_every_case'
    ledger = tmp_path / 'quarantine.json'
    ledger.write_text(json.dumps({
        nodeid: {
            'owner': 'plangraph:FIX-THE-FLAKE',
            'starts': '2026-11-23T09:00:00+00:00',
            'expires': '2026-11-25T09:00:00+00:00',
        },
    }), encoding='utf-8')
    env = dict(_nested_pytest_env(), PYTEST_QUARANTINE_FILE=str(ledger),
               PYTEST_QUARANTINE_AS_OF='2026-11-24T09:00:00+00:00')
    for mode, code, outcome in [('blocking', 5, '1 deselected'), ('quarantine', 0, '1 passed')]:
        result = subprocess.run(
            [sys.executable, '-m', 'pytest', '-q', nodeid], cwd=root,
            env=dict(env, PYTEST_QUARANTINE_MODE=mode),
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == code, result.stdout + result.stderr
        assert outcome in result.stdout
        assert f'QUARANTINED {nodeid} owner=plangraph:FIX-THE-FLAKE mode={mode}' in result.stdout


@pytest.mark.skipif(sys.platform != 'linux', reason='checks an isolated Linux process group')
@pytest.mark.parametrize(('parallel', 'phase'), [(False, 'call'), (True, 'call'), (True, 'setup'), (True, 'teardown')])
def test_pytest_deadline_reports_hang_and_keeps_running(tmp_path, parallel, phase):
    import signal
    import subprocess

    from plangraph.spawner.proc_kill import alive

    root = Path(__file__).resolve().parents[1]
    (tmp_path / 'conftest.py').write_text('pytest_plugins = ("_split_ci_environment",)\n')
    (tmp_path / 'test_hang.py').write_text(
        'from pathlib import Path\n'
        'import os, subprocess, sys, pytest\n'
        'from threading import Event\n\n'
        'def block():\n'
        '    child = subprocess.Popen([sys.executable, "-c", "from threading import Event; Event().wait()"])\n'
        '    Path("child.pid").write_text(str(child.pid))\n'
        '    Path("worker.pid").write_text(str(os.getpid()))\n'
        '    Event().wait()\n'
        '@pytest.fixture\ndef fixture():\n'
        + {'setup': '    block()\n', 'teardown': '    yield\n    block()\n', 'call': '    return block\n'}[phase]
        + f'def test_blocked(fixture):\n    {"fixture()" if phase == "call" else "pass"}\n'
        'def test_after_hang():\n'
        '    assert int(Path("worker.pid").read_text()) == os.getpid()\n',
    )
    (tmp_path / 'test_other_scope.py').write_text('def test_other_scope(): pass\n')
    worker_args = []
    if parallel:
        pytest.importorskip('xdist')
        worker_args = ['-p', 'xdist.plugin', '-n', '2', '--dist', 'loadscope']
    command = subprocess.Popen(
        [sys.executable, '-m', 'pytest', '-q', '-o', 'test_deadline=2', *worker_args, str(tmp_path)],
        cwd=tmp_path, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env=dict(_nested_pytest_env(), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                 PYTHONPATH=os.pathsep.join((str(root / 'tests'), str(root / 'src')))),
    )
    try:
        output, _ = command.communicate(timeout=20)
        assert command.returncode == pytest.ExitCode.TESTS_FAILED, output
        expected = {'call': '1 failed, 2 passed', 'setup': '2 passed, 1 error', 'teardown': '3 passed, 1 error'}
        assert expected[phase] in output and 'INTERNALERROR' not in output
        assert 'HUNG test_hang.py::test_blocked' in output
        assert 'in block' in output
        assert not alive(int((tmp_path / 'child.pid').read_text()), None)
    finally:
        if command.poll() is None:
            os.killpg(command.pid, signal.SIGKILL)
            command.wait(timeout=5)


def _run_without_pthread_kill(tmp_path, body, deadline_signal):
    import subprocess

    root = Path(__file__).resolve().parents[1]
    (tmp_path / 'conftest.py').write_text(
        'import signal\n'
        'pytest_plugins = ("_split_ci_environment",)\n\n'
        'def pytest_configure(config):\n'
        '    import _split_ci_environment as plugin\n'
        '    plugin._PTHREAD_KILL = None\n'
        f'    plugin._DEADLINE_SIGNAL = signal.{deadline_signal}\n',
    )
    (tmp_path / 'test_hang.py').write_text(body)
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-o', 'test_deadline=2', str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env=dict(_nested_pytest_env(), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                 PYTHONPATH=os.pathsep.join((str(root / 'tests'), str(root / 'src')))),
    )
    return result, result.stdout + result.stderr


@pytest.mark.parametrize('deadline_signal', ['SIGUSR2', 'SIGINT'])
@pytest.mark.parametrize('phase', ['call', 'teardown'])
def test_pytest_deadline_interrupts_main_thread_without_pthread_kill(tmp_path, phase, deadline_signal):
    """Windows has no pthread_kill (and carries the deadline on SIGINT): the hung test still fails."""
    import signal

    if getattr(signal, deadline_signal, None) is None:
        pytest.skip(f'{deadline_signal} does not exist on this platform')
    result, output = _run_without_pthread_kill(
        tmp_path,
        'import time, pytest\n\n'
        'def spin():\n'
        '    while True:\n        time.sleep(0.01)\n\n'
        '@pytest.fixture\ndef fixture():\n'
        + {'call': '    return spin\n', 'teardown': '    yield\n    spin()\n'}[phase]
        + f'def test_blocked(fixture):\n    {"fixture()" if phase == "call" else "pass"}\n\n'
        'def test_after_hang():\n    pass\n',
        deadline_signal,
    )
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, output
    assert {'call': '1 failed, 1 passed', 'teardown': '2 passed, 1 error'}[phase] in output, output
    assert 'HUNG test_hang.py::test_blocked' in output and 'in spin' in output, output
    assert 'INTERNALERROR' not in output, output


def test_pytest_deadline_sigint_fallback_keeps_a_real_ctrl_c(tmp_path):
    """Where SIGINT carries the deadline, a user's Ctrl-C still interrupts the session."""
    result, output = _run_without_pthread_kill(
        tmp_path,
        'import os, signal, time\n\n'
        'def test_ctrl_c():\n'
        '    signal.raise_signal(signal.SIGINT)\n'
        '    time.sleep(10)\n\n'
        'def test_not_reached():\n    pass\n',
        'SIGINT',
    )
    assert result.returncode == pytest.ExitCode.INTERRUPTED, output
    assert 'KeyboardInterrupt' in output and 'HUNG' not in output, output


def test_long_test_deadline_covers_a_slow_session_fixture_only_where_marked(tmp_path):
    import subprocess

    root = Path(__file__).resolve().parents[1]
    (tmp_path / 'conftest.py').write_text(
        'import pytest, time\n'
        'pytest_plugins = ("_split_ci_environment",)\n\n'
        '@pytest.fixture(scope="session")\n'
        'def slow_build():\n    time.sleep(4)\n',
    )
    (tmp_path / 'test_marked.py').write_text(
        'import pytest\n\n'
        'pytestmark = pytest.mark.long_test_deadline(30, reason="builds a binary in its fixture")\n\n'
        'def test_marked(slow_build):\n    pass\n',
    )
    (tmp_path / 'test_unmarked.py').write_text(
        'import time\n\n'
        'def test_unmarked_slow():\n    time.sleep(4)\n',
    )
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', '-o', 'test_deadline=2', str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
        env=dict(_nested_pytest_env(), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                 PYTHONPATH=os.pathsep.join((str(root / 'tests'), str(root / 'src')))),
    )
    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, output
    assert 'HUNG test_unmarked.py::test_unmarked_slow' in output, output
    assert 'HUNG test_marked.py' not in output and '1 failed, 1 passed' in output, output


@pytest.mark.skipif(sys.platform != 'linux', reason='checks the thread-directed deadline signal')
@pytest.mark.parametrize('parallel', [False, True])
@pytest.mark.parametrize('patch', [
    'monkeypatch.setattr(threading, "Thread", lambda *a, **k: object())',
    'monkeypatch.setattr(threading.Thread, "daemon", property(lambda self: False, lambda self, value: 1 / 0))',
    'monkeypatch.setattr(threading.Thread, "__init__", lambda self, *a, **k: None)',
])
def test_pytest_deadline_survives_tests_that_patch_threading(tmp_path, parallel, patch):
    import subprocess

    root = Path(__file__).resolve().parents[1]
    (tmp_path / 'conftest.py').write_text('pytest_plugins = ("_split_ci_environment",)\n')
    (tmp_path / 'test_patch.py').write_text(
        'import threading\n\n'
        'def test_patches_threading(monkeypatch):\n'
        f'    {patch}\n\n'
        + ''.join(f'def test_after_{n}():\n    assert threading.Thread.__init__ is not None\n\n' for n in range(4)),
    )
    worker_args = []
    if parallel:
        pytest.importorskip('xdist')
        worker_args = ['-p', 'xdist.plugin', '-n', '2', '--dist', 'load']
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *worker_args, str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
        env=dict(_nested_pytest_env(), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                 PYTHONPATH=os.pathsep.join((str(root / 'tests'), str(root / 'src')))),
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert '5 passed' in output and 'error' not in output.lower(), output


@pytest.mark.skipif(sys.platform != 'linux', reason='checks the thread-directed deadline signal')
def test_pytest_deadline_arming_failure_fails_only_that_test(tmp_path):
    import subprocess

    root = Path(__file__).resolve().parents[1]
    (tmp_path / 'conftest.py').write_text('pytest_plugins = ("_split_ci_environment",)\n')
    (tmp_path / 'test_arm.py').write_text(
        'import _split_ci_environment as plugin\n\n'
        'def test_cannot_arm_teardown(monkeypatch):\n'
        '    def refuse(*args):\n        raise RuntimeError("can\'t start new thread")\n'
        '    monkeypatch.setattr(plugin, "_START_ALARM_THREAD", refuse)\n\n'
        + ''.join(f'def test_after_{n}():\n    pass\n\n' for n in range(3)),
    )
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
        env=dict(_nested_pytest_env(), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',
                 PYTHONPATH=os.pathsep.join((str(root / 'tests'), str(root / 'src')))),
    )
    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, output
    assert '4 passed, 1 error' in output, output
    assert 'could not arm the test deadline' in output and 'INTERNALERROR' not in output, output


def test_modules_importing_a_cargo_building_session_fixture_declare_a_deadline():
    import re

    tests = Path(__file__).resolve().parent
    # These session fixtures run `cargo build` in the setup of the first test that requests them.
    importer = re.compile(r'^from\s+\w+\s+import\b[^\n]*\b(native_client_binary|rules_binary)\b', re.M)
    unmarked = [path.name for path in sorted(tests.glob('test_*.py'))
                if importer.search(path.read_text(encoding='utf-8'))
                and 'long_test_deadline' not in path.read_text(encoding='utf-8')]
    assert not unmarked, f'add a reasoned long_test_deadline override to: {unmarked}'


def _nested_pytest_env():
    return {key: value for key, value in os.environ.items() if not key.startswith('PYTEST_')}
