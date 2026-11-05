"""The writer environment is scrubbed before test fixtures execute."""
import os
from pathlib import Path
import subprocess
import sys
import pytest


def test_writer_variables_are_removed_before_test(tmp_path):
    names = (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "OPENCODE_DISABLE_PROJECT_CONFIG",
        "OPENCODE_DISABLE_CLAUDE_CODE",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
    )
    conftest = Path(__file__).with_name("conftest.py")
    (tmp_path / "conftest.py").write_text(conftest.read_text())
    (tmp_path / "test_env.py").write_text(
        "import os\n"
        "def test_clean():\n"
        f"    assert all(name not in os.environ for name in {names!r})\n"
    )
    env = {**os.environ, **dict.fromkeys(names, "writer-sentinel")}
    env["PYTHONPATH"] = os.pathsep.join((str(conftest.parent), env.get("PYTHONPATH", "")))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-o", "addopts=", str(tmp_path)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
