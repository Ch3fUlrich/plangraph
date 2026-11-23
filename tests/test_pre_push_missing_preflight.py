"""Missing checkout preflight must not silently bypass main's push gate."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize("skip", [False, True])
def test_missing_preflight_main(tmp_path, skip):
    ref = "refs/heads/main"
    root = Path(__file__).resolve().parents[1]
    tools = tmp_path / "tools"
    (tools / "hooks").mkdir(parents=True)
    shutil.copy2(root / "tools/hooks/pre-push", tools / "hooks/pre-push")
    shutil.copytree(root / "tools/split", tools / "split")
    shutil.copy2(root / "tools/ci_commit_gate.py", tools / "ci_commit_gate.py")
    (tools / "check_file_size.py").write_text("raise SystemExit(0)\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-qm", "initial",
                    "--allow-empty"], cwd=tmp_path, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"],
                                   cwd=tmp_path, text=True).strip()
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", head], cwd=tmp_path, check=True)
    # An explicit allowlist also excludes ambient PREFLIGHT_TOOLS_DIR.
    path = os.pathsep.join((str(Path(sys.executable).parent),
                            str(Path(shutil.which("git")).parent), os.defpath))
    env = {"PATH": path, "HOME": str(tmp_path)}
    if skip:
        env["PREFLIGHT_SKIP"] = "1"
    result = subprocess.run(
        [sys.executable, str(tools / "hooks/pre-push")], cwd=tmp_path,
        env=env, input=f"{ref} {head} {ref} {head}\n",
        capture_output=True, text=True)
    assert result.returncode == 1
    diagnostic = ("pre-push: PREFLIGHT_SKIP=1 is not allowed for refs/heads/main: push refused"
                  if skip else "pre-push: tools/preflight.py missing: push refused")
    assert result.stderr.splitlines().count(diagnostic) == 1
