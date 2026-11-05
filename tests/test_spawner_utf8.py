"""Host-independent subprocess decoding, without a live harness."""
import ast
import locale
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from plangraph.spawner.inline_diff import InlineDiffError, inline_diff_prompt


ROOT = Path(__file__).resolve().parents[1] / "src/plangraph/spawner"


def offenders(source):
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords}
        text = keywords.get("text")
        if isinstance(text, ast.Constant) and text.value is True:
            for key, value in (("encoding", "utf-8"), ("errors", "replace")):
                if not isinstance(keywords.get(key), ast.Constant) or keywords[key].value != value:
                    found.append((node.lineno, key))
        if isinstance(node.func, ast.Attribute) and node.func.attr == "decode":
            if not node.args and not node.keywords:
                found.append((node.lineno, "decode"))
    return found


def test_spawner_ast():
    failures = {p.name: offenders(p.read_text(encoding="utf-8")) for p in ROOT.rglob("*.py")}
    assert not {p: errors for p, errors in failures.items() if errors}


def test_ast_detects_wrapper_and_bare_decode():
    assert offenders('self._run([], text=True)\nblob.decode()') == [
        (1, "encoding"), (1, "errors"), (2, "decode")]
    assert not offenders('self._run([], text=True, encoding="utf-8", errors="replace")')


def test_diff_cp1252(tmp_path, monkeypatch):
    # Disable global git configuration and hooks; all commits stay in this temp repo.
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-config"))
    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), "-c", "core.hooksPath=/dev/null", *args])
    git("init", "-q")
    path = tmp_path / "sample.txt"
    path.write_bytes(b"before\n")
    git("add", ".")
    commit = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm")
    git(*commit, "base")
    base = git("rev-parse", "HEAD").decode("ascii").strip()
    path.write_bytes(b"after \x9d\n")
    git("add", ".")
    git(*commit, "head")
    head = git("rev-parse", "HEAD").decode("ascii").strip()
    # _text_encoding uses this locale fallback even on a UTF-8 Linux host.
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp1252")
    monkeypatch.setattr(locale, "getencoding", lambda: "cp1252")
    with pytest.raises(UnicodeDecodeError):
        subprocess.run(["git", "-C", str(tmp_path), "diff", f"{base}...{head}"],
                       capture_output=True, text=True, check=True)
    result = inline_diff_prompt("review", {"repo": str(tmp_path), "base": base, "head": head},
                                max_chars=10000)
    assert "+after \ufffd" in result


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_reader_failure(monkeypatch, tmp_path, stream):
    result = dict(returncode=0, stdout="diff", stderr="")
    result[stream] = None
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(**result))
    with pytest.raises(InlineDiffError, match="reader failure"):
        inline_diff_prompt("review", {"repo": str(tmp_path), "base": "HEAD~1", "head": "HEAD"},
                           max_chars=10000)
