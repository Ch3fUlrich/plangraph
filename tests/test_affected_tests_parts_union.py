"""A train's selection is the affected tests of the union of every part's files (LE-TRAIN-FAST-CHECKS-MISS-CROSS-PART-BREAKS)."""
import json
import os
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools/affected_tests.py"


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.invalid",
                           *args], check=True, capture_output=True, text=True).stdout.strip()


def write(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    git(root, "add", rel)
    git(root, "commit", "-qm", f"edit {rel}")
    return git(root, "rev-parse", "HEAD")


def train(tmp_path):
    """Part left changes a signature; part right owns a test that calls it and was not touched by left."""
    root = tmp_path / "r"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, "src/plangraph/mod.py", "def f(a):\n    return a\n")
    base = write(root, "tests/test_users.py", "from plangraph.mod import f\n\ndef test_f():\n    assert f(1) == 1\n")
    origin = tmp_path / "origin.git"
    git(root, "init", "-q", "--bare", str(origin))
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-q", "origin", "main")
    git(root, "checkout", "-qb", "left", base)
    write(root, "src/plangraph/mod.py", "def f(a, b):\n    return a + b\n")
    git(root, "push", "-q", "origin", "left")
    git(root, "checkout", "-qb", "right", base)
    write(root, "docs/note.txt", "right part\n")
    git(root, "push", "-q", "origin", "right")
    git(root, "checkout", "-qb", "train", base)
    git(root, "merge", "--no-ff", "-q", "left", "-m", "part left")
    git(root, "merge", "--no-ff", "-q", "right", "-m", "part right")
    git(root, "fetch", "-q", "origin")
    return root, base, git(root, "rev-parse", "HEAD")


def select(root, base, head, *extra):
    env = dict(os.environ, PLANGRAPH_PUSH_RANGE=f"{base}:{head}")
    out = subprocess.run([sys.executable, str(TOOL), "--root", str(root), "--base", base, "--head", head,
                          "--files-only", "--local", *extra], env=env, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.splitlines()[0])["tests"], out.stderr


def test_parts_union_selects_the_test_that_a_part_breaks_outside_the_changed_files(tmp_path):
    root, base, head = train(tmp_path)
    tests, err = select(root, base, head, "--parts-union")
    assert "tests/test_users.py" in tests
    assert "mode=pushed-parts parts=2" in err


def test_without_the_flag_the_interaction_selection_stays_as_it_was(tmp_path):
    root, base, head = train(tmp_path)
    tests, _ = select(root, base, head)
    assert "tests/test_users.py" not in tests
