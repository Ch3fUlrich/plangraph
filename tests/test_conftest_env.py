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
