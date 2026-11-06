"""Released replay evidence is data, not a replaceable recording cache."""
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = "rust/worker/tests/fixtures/releases"


def reject_changes(base, root=ROOT):
    """CI entry point after Gate-Edit: only additions under a new build are legal."""
    result = subprocess.run(
        ["git", "diff", "--name-status", "--no-renames", base, "--", ARCHIVE],
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


if __name__ == "__main__":
    import sys
    violations = reject_changes(sys.argv[1])
    print("\n".join(violations))
    raise SystemExit(bool(violations))
