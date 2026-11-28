"""Skill text and docs never start the full suite (D-7122): a mixed or unknown path still does."""
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
])
def test_skill_text_selects_only_pytest(paths, full, rust):
    res = cs.select(paths)
    assert res["full"] is full and res["rust"] is rust and res["pytest"] is True
    assert full or rust or not any(res[s] for s in cs.SUITES if s != "pytest")


@pytest.mark.parametrize("changed,merge,skipped", [
    (["skills/x/SKILL.md", "skills/x/evals/evals.json"], True, True),
    (["skills/x/SKILL.md", "src/plangraph/m.py"], True, False),
    (["skills/x/SKILL.md"], False, False),
    (["skills/batch-writer/SKILL.md", "docs/x.md"], True, False),
    (["docs/schemas/a.json"], True, False),
])
def test_push_skips_heavy_jobs_only_for_a_text_only_merge(tmp_path, monkeypatch, capsys, changed, merge, skipped):
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True).stdout
    git("init", "-q", "-b", "main"); git("config", "user.email", "t@example.invalid"); git("config", "user.name", "t")
    git("commit", "-q", "--allow-empty", "-m", "base"); git("checkout", "-q", "-b", "lane")
    for p in changed:
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True); (tmp_path / p).write_text("y\n")
    git("add", "-A"); git("commit", "-q", "-m", "change"); git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "merge", "lane")
    monkeypatch.chdir(tmp_path); monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("GITHUB_SHA", git("rev-parse", "HEAD" if merge else "HEAD^2").strip())
    assert cs.main(["select", "--event", "push"]) == 0
    out = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    assert (out.get("tested") == "true" and out["full"] == "false") is skipped
    outs = {s: "false" for s in cs.SUITES} | {"full": "false", "tested": "true"}
    needs = {"select": {"result": "success", "outputs": outs}} | {j: {"result": "success"} for j in cs.ALWAYS}
    assert cs.gate(needs, require_full=True) == []
