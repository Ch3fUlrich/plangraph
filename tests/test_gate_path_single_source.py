"""LE-GATE-PATH-LIST-COPIED-IN-HELPERS: GATE_PATHS is spelled only in tools/ci_commit_gate.py; others read it."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import ci_commit_gate  # noqa: E402

SOURCE = "tools/ci_commit_gate.py"
# Code and briefs are scanned; tests and prose may name gate paths one by one.
SCANNED = ("tools/", "rust/", "skills/", "deploy/", "src/", "setup/")
# Gate paths themselves cannot change without a router Gate-Edit D-id; the verifier keeps its own
# operator-class table (it adds the CI matrix) until a Gate-Edit grant lets it read GATE_PATHS.
ALLOWED = {"rust/plangraph-cli/src/land/go_verify.rs"}
# A file that spells this many live entries holds a copy of the list.
COPY_THRESHOLD = 4


def needles():
    return sorted({p.rstrip("*").lstrip("*/") for p in ci_commit_gate.GATE_PATHS})


def is_test(path):
    name = Path(path).name
    return "/tests/" in f"/{path}" or name.startswith("test_") or name.endswith("_tests.rs")


def copies(files):
    live = needles()
    found = []
    for path in files:
        if path == SOURCE or path in ALLOWED or is_test(path) or not path.startswith(SCANNED):
            continue
        try:
            text = (ROOT / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if sum(needle in text for needle in live) >= COPY_THRESHOLD:
            found.append(path)
    return found


def tracked():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def test_no_file_outside_the_gate_spells_the_gate_list_by_hand():
    assert copies(tracked()) == []


def test_the_scan_catches_a_hand_copied_list(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    (tmp_path / "tools").mkdir()
    copied = tmp_path / "tools/push_helper.sh"
    copied.write_text("GATES='" + " ".join(needles()[:COPY_THRESHOLD]) + "'\n")
    reads = tmp_path / "tools/reader.py"
    reads.write_text("from ci_commit_gate import GATE_PATHS\n")
    assert copies(["tools/push_helper.sh", "tools/reader.py"]) == ["tools/push_helper.sh"]


def test_gate_paths_has_the_entries_the_brief_builders_must_print():
    # Seven entries on 2026-11-24; a smaller tuple means a path left the gate and the briefs shrink with it.
    assert len(ci_commit_gate.GATE_PATHS) >= 7
