"""The writer environment is scrubbed before test fixtures execute."""
import os
from pathlib import Path
import pytest
from conftest import hermetic_writer_env


def test_writer_variables_are_removed_before_test(monkeypatch):
    names = (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "OPENCODE_DISABLE_PROJECT_CONFIG",
        "OPENCODE_DISABLE_CLAUDE_CODE",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
    )
    for name in names:
        monkeypatch.setenv(name, "writer-sentinel")
    hermetic_writer_env.__wrapped__(monkeypatch)
    for name in names:
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
