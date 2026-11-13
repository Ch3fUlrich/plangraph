"""The writer environment is scrubbed before test fixtures execute."""
import os
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


def test_session_git_configuration_is_isolated():
    assert os.environ.get('GIT_CONFIG_NOSYSTEM') == '1'
    path = Path(os.environ['GIT_CONFIG_GLOBAL'])
    assert path.name == 'global.gitconfig' and path.is_file()


@pytest.mark.parametrize('worktree', [False, True])
def test_git_config_guard_detects_change_at_teardown(tmp_path, worktree):
    import conftest
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
        with conftest.git_config_guard(repo):
            config.write_text('[core]\n bare = true\n')
