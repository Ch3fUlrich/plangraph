"""The writer environment is scrubbed before test fixtures execute."""
import os
from pathlib import Path
import subprocess
import sys


def test_writer_variables_are_removed_before_test(tmp_path):
    names = (
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
