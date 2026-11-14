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
