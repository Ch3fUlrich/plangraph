"""Missing checkout preflight must not silently bypass main's push gate."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("skip", [False, True])
def test_missing_preflight_main(tmp_path, skip):
    root = Path(__file__).resolve().parents[1]
    tools = tmp_path / "tools"
    (tools / "hooks").mkdir(parents=True)
    shutil.copy2(root / "tools/hooks/pre-push", tools / "hooks/pre-push")
    shutil.copytree(root / "tools/split", tools / "split")
    (tools / "check_file_size.py").write_text("raise SystemExit(0)\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "initial",
                    "--allow-empty"], cwd=tmp_path, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                   cwd=tmp_path, text=True).strip()
    env = dict(os.environ, HOME=str(tmp_path))
    for key in ("PREFLIGHT_SKIP", "PLANGRAPH_SCRUB_WORDS", "PLANGRAPH_SCRUB_RULES"):
        env.pop(key, None)
    if skip:
        env["PREFLIGHT_SKIP"] = "1"
    result = subprocess.run(
        [sys.executable, str(tools / "hooks/pre-push")], cwd=tmp_path,
        env=env, input=f"refs/heads/main {head} refs/heads/main {head}\n",
        capture_output=True, text=True)
    assert result.returncode == (0 if skip else 1)
    reason = ("pre-push: PREFLIGHT_SKIP=1: preflight skipped (other gates still run)"
              if skip else "pre-push: tools/preflight.py missing: push refused")
    assert result.stderr.splitlines().count(reason) == 1
