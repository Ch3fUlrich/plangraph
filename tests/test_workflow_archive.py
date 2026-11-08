"""Released replay evidence is data, not a replaceable recording cache."""
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = "rust/worker/tests/fixtures/releases"
FIXTURES = "rust/worker/tests/fixtures"


def reject_changes(base, root=ROOT):
    """CI entry point after Gate-Edit: only additions under a new build are legal."""
    result = subprocess.run(
        ["git", "diff", "--name-status", "--no-renames", base, "--", FIXTURES],
        cwd=root, text=True, capture_output=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if not line.startswith("A\t")]


def validate_pins(fixtures):
    for path in (fixtures / "releases").glob("*/manifest.json"):
        manifest = json.loads(path.read_text())
        assert manifest["source_commit"]
        for name, digest in manifest["sha256"].items():
            assert any(candidate.is_file() and
                       hashlib.sha256(candidate.read_bytes()).hexdigest() == digest
                       for candidate in (fixtures / name, path.parent / name)), name


def test_workflow_archive_runs_guarded_base_checker():
    import shlex
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    step, = (step for job in workflow["jobs"].values()
             for step in job.get("steps", [])
             if step.get("name") == "Workflow archive policy")
    run = step["run"]
    assert 'git rev-parse --verify --end-of-options "HEAD^1^{commit}"' in run
    commands = shlex.split(run)
    assert commands == [
        "b=$(git", "rev-parse", "--verify", "--end-of-options",
        "HEAD^1^{commit})", "&&",
        "git", "show", "$b:tests/test_workflow_archive.py",
        ">", ".git/workflow_archive_base.py", "&&", "python",
        ".git/workflow_archive_base.py", "$b",
    ]
    assert "tests/test_workflow_archive.py" not in commands


def test_workflow_archive_never_uses_stale_pr_base_sha():
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    steps = workflow["jobs"]["pytest"]["steps"]
    checkout, = (step for step in steps
                 if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0
    step, = (step for step in steps
             if step.get("name") == "Workflow archive policy")
    assert step["if"] == "github.event_name == 'pull_request'"
    assert "BASE_SHA" not in step["run"]
    assert "base.sha" not in step["run"]
    assert '"HEAD^1^{commit}"' in step["run"]


def test_released_archive_matches_manifest():
    validate_pins(ROOT / "rust/worker/tests/fixtures")


def test_changed_fixture_requires_old_copy(tmp_path):
    import pytest
    release = tmp_path / "releases" / "test"
    release.mkdir(parents=True)
    (release / "manifest.json").write_text(json.dumps({
        "source_commit": "test", "sha256": {
            "happy.json": hashlib.sha256(b"old").hexdigest()}}))
    (tmp_path / "happy.json").write_bytes(b"new")
    with pytest.raises(AssertionError):
        validate_pins(tmp_path)


def test_changed_fixture_with_old_copy_passes(tmp_path):
    release = tmp_path / "releases" / "test"
    release.mkdir(parents=True)
    (release / "manifest.json").write_text(json.dumps({
        "source_commit": "test", "sha256": {
            "happy.json": hashlib.sha256(b"old").hexdigest()}}))
    (tmp_path / "happy.json").write_bytes(b"new")
    (release / "happy.json").write_bytes(b"old")
    validate_pins(tmp_path)


def test_archive_policy_rejects_edit_and_delete(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True,
                              capture_output=True, text=True)
    git("init")
    path = tmp_path / ARCHIVE / "old" / "happy.json"
    path.parent.mkdir(parents=True)
    path.write_text("old")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "baseline")
    path.write_text("edited")
    assert reject_changes("HEAD", tmp_path)
    path.unlink()
    assert reject_changes("HEAD", tmp_path)
    path.write_text("old")
    added = path.parent.parent / "new" / "happy.json"
    added.parent.mkdir()
    added.write_text("new")
    git("add", ".")
    assert not reject_changes("HEAD", tmp_path)


def test_policy_rejects_top_level_rerecord_even_with_archived_copy(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, check=True,
                              capture_output=True, text=True)
    git("init")
    path = tmp_path / FIXTURES / "cancel.json"
    path.parent.mkdir(parents=True)
    path.write_text("old")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-m", "baseline")
    old = tmp_path / ARCHIVE / "old" / path.name
    old.parent.mkdir(parents=True)
    old.write_text("old")
    path.write_text("new")
    git("add", ".")
    assert reject_changes("HEAD", tmp_path) == [f"M\t{FIXTURES}/cancel.json"]


if __name__ == "__main__":
    import sys
    violations = reject_changes(sys.argv[1])
    print("\n".join(violations))
    raise SystemExit(bool(violations))
