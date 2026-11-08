"""Offline fixtures for complete findings and explicit file cuts."""
import subprocess

from tools.judge_prompt import build_judge_prompt, cited_paths, collect_files, main


def test_cited_outside_touched_and_budget(tmp_path):
    (tmp_path / "touched.py").write_text("changed\n")
    (tmp_path / "other.py").write_text("évidence\n")
    findings = "F1 HIGH `other.py:1` broken\nF2 LOW touched.py:2 also broken"
    files = collect_files(tmp_path, ["touched.py", *cited_paths(findings)])
    assert list(files) == ["touched.py", "other.py"]
    complete = build_judge_prompt(findings, "diff", files, {"unit": "success"})
    assert "## File: other.py\névidence" in complete
    cut = build_judge_prompt(findings, "diff", files, {}, byte_budget=0)
    assert findings in cut
    assert "- touched.py (byte budget)" in cut
    assert "- other.py (byte budget)" in cut
    assert "Read/Grep/Glob" not in cut
    assert all(word in cut for word in ["UPHELD|VOID", "NEW HIGH", "VERDICT"])


def test_unavailable_and_multibyte_budget(tmp_path):
    files = collect_files(tmp_path, ["missing.py", "../outside.py"])
    files["unicode.py"] = "é"
    prompt = build_judge_prompt("ALL FINDINGS", "diff", files, {}, byte_budget=1)
    assert "ALL FINDINGS" in prompt
    assert all(f"- {name} (" in prompt for name in files)


def test_cli_local_git(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init")
    (tmp_path / "a.py").write_text("old\n")
    (tmp_path / "cited.py").write_text("untouched\n")
    (tmp_path / "directory").mkdir()
    (tmp_path / "directory" / "child.py").write_text("child\n")
    git("add", ".")
    # Per-command identity: no git config and no external service.
    def commit(message):
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "core.hooksPath=/dev/null", "commit", "-m", message)
    commit("base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "a.py").write_text("new\n")
    (tmp_path / "raw.py").write_bytes(b"non-utf8: \xff\n")
    git("add", ".")
    commit("head")
    findings = tmp_path / "findings.txt"
    findings.write_text("F1 HIGH cited.py:1 defect\nF2 directory/:1 defect")
    ci = tmp_path / "ci.json"
    ci.write_text('{"tests": "success"}')
    output = tmp_path / "prompt.txt"
    assert main(["--repo", str(tmp_path), "--findings", str(findings), "--base", base,
                 "--head", "HEAD", "--ci-summary", str(ci), "--output", str(output)]) == 0
    prompt = output.read_text()
    assert "## File: cited.py\nuntouched" in prompt
    assert "## File: a.py\nnew" in prompt
    assert '+new' in prompt and '"tests": "success"' in prompt
    assert "non-utf8: �" in prompt
    assert "## File: directory/\n" not in prompt
    assert "- directory/ (unavailable or deleted)" in prompt


def test_judge_seat_mode():
    prompt = build_judge_prompt("F1", "diff", {"a.py": "body"}, {}, byte_budget=0)
    assert "Cut files are unavailable" in prompt
    assert "Read" not in prompt and "Grep" not in prompt
    tools = build_judge_prompt("F1", "diff", {}, {}, tool_mode="tools")
    assert "Read/Grep/Glob" in tools


def test_citations_and_priority():
    findings = "https://example.invalid/a.py:2 --flag:3 name:4 `cited.py:1` src/mod.py:2"
    assert cited_paths(findings) == ["cited.py", "src/mod.py"]
    prompt = build_judge_prompt(findings, "diff", {"touched.py": "abc", "cited.py": "xyz"},
                                {}, byte_budget=3)
    assert "## File: cited.py\nxyz" in prompt
    assert "- touched.py (byte budget)" in prompt


def test_docs_scope():
    from pathlib import Path

    docs = Path("docs/SPAWNER.md").read_text()
    assert "Lean review harness" not in docs
    assert "five-minute freshness bound,\nnot a five-minute completion" in docs
    assert "Terminal history\ndoes not consume" in docs
    changes = Path("changelog.d/REVIEW-HARNESS-LEAN.md").read_text()
    assert "Split runner" not in changes
    assert "Runner structure is unchanged" in changes
