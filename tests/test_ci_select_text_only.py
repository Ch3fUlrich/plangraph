"""Skill text and docs never start the full suite (D-7122): a mixed, renamed, unknown or non-Markdown path still does."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("cs_text", Path(__file__).resolve().parents[1] / "tools/ci_select.py")
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)


@pytest.mark.parametrize("paths,full,rust", [
    (["skills/spec-creation/evals/evals.json", "skills/spec-creation/SKILL.md", "AGENTS.md"], False, False),
    (["skills/batch-writer/SKILL.md"], False, True),
    (["skills/routing-registry/scripts/x.sh"], True, True),
    (["skills/spec-creation/evals/requirements.json"], True, True),
    (["skills/spec-creation/evals/deep/evals.json"], True, True),
    (["skills/spec-creation/evals/evals.json", "src/plangraph/review/prompt.py"], True, True),
])
def test_skill_text_selects_only_pytest(paths, full, rust):
    res = cs.select(paths)
    assert res["full"] is full and res["rust"] is rust and res["pytest"] is True
    assert full or rust or not any(res[s] for s in cs.SUITES if s != "pytest")


@pytest.mark.parametrize("changed,merge,skipped", [
    (["skills/x/SKILL.md", "skills/x/evals/evals.json", "docs/a.md"], True, True),
    (["skills/x/SKILL.md", "src/plangraph/m.py"], True, False),
    (["skills/x/SKILL.md"], False, False),
    (["skills/batch-writer/SKILL.md", "docs/x.md"], True, False),
    (["docs/schemas/a.json"], True, False),
    (["docs/plan/tool.py"], True, False),
    (["README.md CHANGELOG"], True, False),
    (["rename:src/plangraph/m.py>docs/m.md"], True, False),
])
def test_push_skips_heavy_jobs_only_for_a_text_only_merge(tmp_path, monkeypatch, capsys, changed, merge, skipped):
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True).stdout
    def put(p):
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True); (tmp_path / p).write_text("same content\n" * 20)
    git("init", "-q", "-b", "main"); git("config", "user.email", "t@example.invalid"); git("config", "user.name", "t")
    renames = [c[7:].split(">") for c in changed if c.startswith("rename:")]
    for old, _ in renames:
        put(old)
    git("add", "-A"); git("commit", "-q", "--allow-empty", "-m", "base"); git("checkout", "-q", "-b", "lane")
    for old, new in renames:
        (tmp_path / new).parent.mkdir(parents=True, exist_ok=True); git("mv", old, new)
    for p in (c for c in changed if not c.startswith("rename:")):
        put(p)
    git("add", "-A"); git("commit", "-q", "-m", "change"); git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "merge", "lane")
    monkeypatch.chdir(tmp_path); monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("GITHUB_SHA", git("rev-parse", "HEAD" if merge else "HEAD^2").strip())
    assert cs.main(["select", "--event", "push"]) == 0
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert {out[s] for s in cs.SUITES} == ({"false"} if skipped else {"true"})
    assert (out.get("tested"), out["full"]) == (("true", "false") if skipped else (None, "true"))
    needs = {"select": {"result": "success", "outputs": out}} | {j: {"result": "success"} for j in cs.ALWAYS}
    needs |= {s: {"result": "success"} for s in (*cs.SUITES, "rust-windows")} if not skipped else {}
    assert cs.gate(needs, require_full=True) == []


def _repo(tmp_path):
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True).stdout.decode()
    git("init", "-q", "-b", "main"); git("config", "user.email", "t@example.invalid"); git("config", "user.name", "t")
    (tmp_path / "base.md").write_text("base\n"); git("add", "-A"); git("commit", "-q", "-m", "base")
    return git


def test_a_path_that_is_not_utf8_runs_the_full_suite(tmp_path, monkeypatch):
    git = _repo(tmp_path)
    git("checkout", "-q", "-b", "lane")
    (tmp_path / "docs").mkdir(); (tmp_path / "docs" / "ok.md").write_text("x\n")
    with open(bytes(tmp_path / "docs") + b"/\xff.md", "w") as handle:
        handle.write("x\n")
    git("add", "-A"); git("commit", "-q", "-m", "bytes"); git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "merge", "lane")
    monkeypatch.chdir(tmp_path)
    assert cs._text_only_merge(git("rev-parse", "HEAD").strip()) is None


@pytest.mark.parametrize("second,skipped", [("docs/b.md", True), ("src/plangraph/m.py", False)])
def test_an_octopus_merge_is_text_only_only_when_every_branch_is_text(tmp_path, monkeypatch, second, skipped):
    git = _repo(tmp_path)
    for lane, path in (("a", "docs/a.md"), ("b", second)):
        git("checkout", "-q", "-b", lane, "main")
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True); (tmp_path / path).write_text(lane + "\n")
        git("add", "-A"); git("commit", "-q", "-m", lane)
    git("checkout", "-q", "main"); git("merge", "-q", "--no-ff", "-m", "octopus", "a", "b")
    assert len(git("rev-list", "--parents", "-n1", "HEAD").split()) == 4
    monkeypatch.chdir(tmp_path)
    assert (cs._text_only_merge(git("rev-parse", "HEAD").strip()) == "text-only merge") is skipped


def test_a_pull_request_renaming_code_into_docs_still_selects_its_suite(tmp_path, monkeypatch, capsys):
    git = _repo(tmp_path)
    (tmp_path / "src/plangraph").mkdir(parents=True); (tmp_path / "src/plangraph/m.py").write_text("same\n" * 20)
    git("add", "-A"); git("commit", "-q", "-m", "code"); base = git("rev-parse", "HEAD").strip()
    git("checkout", "-q", "-b", "lane"); (tmp_path / "docs").mkdir(); git("mv", "src/plangraph/m.py", "docs/m.md")
    git("commit", "-q", "-m", "rename"); head = git("rev-parse", "HEAD").strip()
    monkeypatch.chdir(tmp_path); monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert cs.main(["select", "--event", "pull_request", "--base", base, "--head", head]) == 0
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert out["pytest"] == "true" and "tested" not in out
