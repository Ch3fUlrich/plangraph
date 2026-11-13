import importlib.util
from pathlib import Path

import pytest


def selector():
    spec = importlib.util.spec_from_file_location(
        "affected_tests", Path(__file__).resolve().parents[1] / "tools/affected_tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _repo(tmp_path, extra=None):
    files = {"src/pkg/core.py": "VALUE = 1", "tests/test_core.py": "from pkg import core",
             "changelog.d/FRAG.md": "x", "CHANGELOG.md": "x", "notes/readme.md": "x",
             "notes/ideas.txt": "x", "notes/tool.sh": "x", **(extra or {})}
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    import subprocess
    for cmd in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", "-C", str(tmp_path), *cmd], check=True)


@pytest.mark.parametrize("path", ["changelog.d/FRAG.md", "CHANGELOG.md", "notes/readme.md",
                                  "notes/ideas.txt", "docs/x.md"])
def test_docs_and_changelog_fragments_select_no_tests(tmp_path, path):
    _repo(tmp_path)
    assert selector().select_tests(tmp_path, [path])["tests"] == []


def test_unmapped_tracked_non_doc_file_still_falls_back(tmp_path):
    _repo(tmp_path)
    assert selector().select_tests(tmp_path, ["notes/tool.sh"])["tests"] == ["tests"]


def test_unmapped_source_file_still_falls_back(tmp_path):
    _repo(tmp_path, {"src/pkg/orphan.py": ""})
    assert selector().select_tests(tmp_path, ["src/pkg/orphan.py"])["tests"] == ["tests"]
