"""Gap tests for md_sync router D-row pass (R5 - R9)."""
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from git_identity import identity_env

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import apply_batch as ab  # noqa: E402
import md_sync  # noqa: E402
import md_sync_dclose as dclose  # noqa: E402
import md_sync_parse as mp  # noqa: E402
import md_sync_plan as plan  # noqa: E402
from plangraph.keeper.evidence_decisions import DecisionsAdapter  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    """A temp git repo: one commit on main (merged), one on a side branch."""
    path = tmp_path / "repo"
    path.mkdir()

    def run(*args):
        out = subprocess.run(["git", *args], cwd=path, capture_output=True,
                             text=True, env=identity_env("synthetic", "synthetic@example.invalid"))
        assert out.returncode == 0, out.stderr
        return out

    run("init", "-q", "-b", "main")
    (path / "a.txt").write_text("alpha\n", encoding="utf-8")
    run("add", "a.txt")
    run("commit", "-q", "-m", "alpha")
    merged = run("rev-parse", "HEAD").stdout.strip()
    run("checkout", "-q", "-b", "side")
    (path / "b.txt").write_text("beta\n", encoding="utf-8")
    run("add", "b.txt")
    run("commit", "-q", "-m", "beta")
    unmerged = run("rev-parse", "HEAD").stdout.strip()
    run("checkout", "-q", "main")
    return SimpleNamespace(path=str(path), merged=merged, unmerged=unmerged)


def repos(repo, ref="main"):
    return {"demo": {"path": repo.path, "ref": ref}}


def dec_row(d, decision, closes=None, by="router", project="demo"):
    cell = decision + (f" Closes: {closes}" if closes else "")
    return f"| {d} | {project} | {cell} | {by} | why |\n"


def decs(tmp_path, *rows):
    text = ("# Decisions (SYNTHETIC fixture, not site data)\n\n"
            "| ID | project | decision | by | why |\n|---|---|---|---|---|\n"
            + "".join(rows))
    path = tmp_path / "decisions-log.md"
    path.write_text(text, encoding="utf-8")
    return text, DecisionsAdapter(path)


def item(ident, state="open", tier="standard", kinds=("any",), note=None,
         evidence=(), project="demo"):
    return {"project": project, "id": ident, "key": f"{project}:{ident}",
            "state": state, "review_tier": tier, "evidence_kinds": list(kinds),
            "note": note, "evidence": [dict(e) for e in evidence],
            "depends_on": []}


def build(items, text, view, repo, since=1, answers=(), ids=None,
          repos_dict=None):
    return plan.build_plan([], list(answers), [], ids or set(), items, view,
                           decisions_text=text,
                           repos=repos_dict if repos_dict is not None else repos(repo),
                           since=since)


def test_gap_r9_pipe_inside_models_segment_drops_row(repo, tmp_path):
    # A | in a cell splits it across columns, so keeper drops the row before md_sync sees it
    row = "| D-505 | demo | work Models: foo | bar Closes: demo:CODE-1 | router | why |\n"
    text, view = decs(tmp_path, row)
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)
    assert p.ops == []
    assert not any("demo:CODE-1" in line for line in p.d_lines)


def test_gap_r9_mixed_row_good_and_invalid_tokens(repo, tmp_path):
    text, view = decs(tmp_path,
                      dec_row("D-505", "work",
                              f"demo:CODE-1@{repo.merged[:8]} demo:BAD@zzz"))
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)
    assert any(o["op"] == "close_on_decision" and o.get("key") == "demo:CODE-1"
               for o in p.ops)
    assert not any("demo:BAD" in o.get("key", "") for o in p.ops)
    out = io.StringIO()
    md_sync.report(p, out, dry=True)
    report_text = out.getvalue()
    assert "close demo:CODE-1: by D-505" in report_text
    assert "invalid Closes token in D-505" not in report_text


def test_gap_r6_second_run_zero_ops_and_events(repo, tmp_path):
    import hashlib
    from keeper_util import World, seed
    from plangraph.keeper.evidence_git import GitAdapter, LegacyCommitVerifier

    kdb = tmp_path / "kdb"
    w = World(kdb)
    seed(w, [("CODE-1", []), ("CODE-2", []), ("CODE-3", []),
             ("CLAIM-1", []), ("DEC-1", [])],
         project="demo", evidence_kinds=("commit",))
    w.store.apply_ops(
        w.p("router"),
        [{"op": "edit", "key": "demo:DEC-1",
          "fields": {"review_tier": "none", "evidence_kinds": ["decision"]}}],
        "init@0000000000000000000000000000000000000000000000000000000000000000")
    w.store.claim(w.p("workerA"), "demo:CLAIM-1")

    w.store.verifiers["commit"] = LegacyCommitVerifier()
    w.store.ctx = SimpleNamespace(
        decisions=None, git=GitAdapter(repos(repo)))
    w.store.batch_roots = [str(tmp_path)]

    d_text = (
        dec_row("D-501", "one Models: modelA", f"demo:CODE-1@{repo.merged[:8]}")
        + dec_row("D-502", "two Models: modelB", f"demo:CODE-2@{repo.unmerged[:8]}")
        + dec_row("D-503", "three", "demo:CODE-3")
        + dec_row("D-504", "four Models: modelC", f"demo:CLAIM-1@{repo.merged[:8]}")
        + dec_row("D-505", "five", "demo:DEC-1")
    )
    d_path = tmp_path / "decisions.md"
    d_path.write_text(d_text, encoding="utf-8")
    view = DecisionsAdapter(d_path)
    w.store.ctx.decisions = view

    live1 = w.store.snapshot(w.p("router"))["items"]
    p1 = plan.build_plan([], [], [], {"D-501", "D-502", "D-503", "D-504", "D-505"},
                         live1, view, decisions_text=d_text, repos=repos(repo))
    assert len(p1.ops) > 0
    batch1 = tmp_path / "batch1.yaml"
    batch1.write_text(json.dumps({"ops": p1.ops}), encoding="utf-8")
    dig1 = hashlib.sha256(batch1.read_bytes()).hexdigest()
    w.store.apply_ops(w.p("router"), p1.ops, f"{batch1}@{dig1}")

    events_after_1 = w.conn.execute("SELECT count(*) FROM events").fetchone()[0]

    live2 = w.store.snapshot(w.p("router"))["items"]
    p2 = plan.build_plan([], [], [], {"D-501", "D-502", "D-503", "D-504", "D-505"},
                         live2, view, decisions_text=d_text, repos=repos(repo))
    assert len(p2.ops) == 0

    if p2.ops:
        batch2 = tmp_path / "batch2.yaml"
        batch2.write_text(json.dumps({"ops": p2.ops}), encoding="utf-8")
        dig2 = hashlib.sha256(batch2.read_bytes()).hexdigest()
        w.store.apply_ops(w.p("router"), p2.ops, f"{batch2}@{dig2}")

    events_after_2 = w.conn.execute("SELECT count(*) FROM events").fetchone()[0]
    assert events_after_2 == events_after_1


def test_gap_r7_dedupe_across_answers_and_drows(repo, tmp_path):
    text, view = decs(
        tmp_path,
        dec_row("D-505", "ship code one", f"demo:CODE-1@{repo.merged[:8]}"),
        dec_row("D-515", "follow up", f"demo:CODE-1@{repo.merged[:8]}"),
    )
    ans = [mp.Answer("CODE-1", ["D-505", "D-515"], "2026-10-01", "done")]
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo,
              answers=ans, ids={"D-505", "D-515"})
    assert len([o for o in p.ops if o["op"] == "close_on_decision"]) == 1
    dec_ev = [o["ref"]["id"] for o in p.ops
              if o["op"] == "add_evidence" and o.get("kind") == "decision"]
    assert set(dec_ev) == {"D-505", "D-515"}


def test_gap_r7_decisions_since_through_cli(repo, tmp_path):
    q_file = tmp_path / "questions.md"
    q_file.write_text("# Questions\n", encoding="utf-8")
    a_file = tmp_path / "answers.md"
    a_file.write_text("# Answers\n", encoding="utf-8")
    d_file = tmp_path / "decisions.md"
    d_text = (
        "# Decisions\n\n| ID | project | decision | by | why |\n|---|---|---|---|---|\n"
        + dec_row("D-501", "low", f"demo:CODE-1@{repo.merged[:8]}")
        + dec_row("D-505", "high", f"demo:CODE-2@{repo.merged[:8]}")
    )
    d_file.write_text(d_text, encoding="utf-8")
    tok_file = tmp_path / "token.txt"
    tok_file.write_text("secret\n", encoding="utf-8")
    kcfg_file = tmp_path / "keeper.json"
    kcfg_file.write_text(json.dumps({
        "repos": {"demo": {"path": repo.path, "ref": "main"}}
    }), encoding="utf-8")

    view = DecisionsAdapter(d_file)
    items = [item("CODE-1", kinds=("commit",)), item("CODE-2", kinds=("commit",))]
    p = plan.build_plan([], [], [], set(), items, view,
                         decisions_text=d_text, repos=repos(repo), since=502)
    assert not any(o.get("key") == "demo:CODE-1" for o in p.ops)
    assert any(o.get("key") == "demo:CODE-2" for o in p.ops)


def _repo_dual(repo):
    p = repo.path
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "g"], cwd=p, check=True)
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=p, capture_output=True, text=True, check=True)
    subprocess.run(["git", "update-ref", "refs/remotes/origin/main", repo.unmerged], cwd=p, check=True)
    return SimpleNamespace(path=p, main=out.stdout.strip(), origin=repo.unmerged)


def test_gap_r7_repo_ref_origin_main_and_side_branch(repo, tmp_path):
    # (d) side branch closes only under 'side', stays in_review under origin/main & main
    text, view = decs(tmp_path, dec_row("D-505", "work", f"demo:CODE-1@{repo.unmerged[:8]}"))
    for ref in ("origin/main", "main"):
        p = build([item("CODE-1", kinds=("commit",))], text, view, repo,
                  repos_dict={"demo": {"path": repo.path, "ref": ref}})
        assert not [o for o in p.ops if o["op"] in ("close_on_decision", "add_evidence")]
        assert any(r["reason"] == f"not on {ref}" for r in p.d_in_review)
    p_side = build([item("CODE-1", kinds=("commit",))], text, view, repo,
                   repos_dict={"demo": {"path": repo.path, "ref": "side"}})
    assert p_side.d_closes == [{"key": "demo:CODE-1", "decision": "D-505"}]


def test_gap_r7_configured_ref_decides_merged_closes(repo, tmp_path):
    # (a) the configured ref decides: a commit merged into configured ref closes
    dr = _repo_dual(repo)
    for ref, sha, d in (("origin/main", dr.origin, "D-505"), ("main", dr.main, "D-506")):
        t, v = decs(tmp_path, dec_row(d, "work", f"demo:CODE@{sha[:8]}"))
        p = build([item("CODE", kinds=("commit",))], t, v, dr,
                  repos_dict={"demo": {"path": dr.path, "ref": ref}})
        assert p.d_closes == [{"key": "demo:CODE", "decision": d}]


def test_gap_r7_commit_only_on_local_main_stays_in_review(repo, tmp_path):
    # (b) config origin/main with commit only on local main stays in_review
    dr = _repo_dual(repo)
    t, v = decs(tmp_path, dec_row("D-505", "work", f"demo:CODE-1@{dr.main[:8]}"))
    p = build([item("CODE-1", kinds=("commit",))], t, v, dr,
              repos_dict={"demo": {"path": dr.path, "ref": "origin/main"}})
    assert not [o for o in p.ops if o["op"] in ("close_on_decision", "add_evidence")]
    assert any(r["reason"] == "not on origin/main" for r in p.d_in_review)


def test_gap_r7_commit_only_on_origin_main_stays_in_review(repo, tmp_path):
    # (b) config main with commit only on origin/main stays in_review
    dr = _repo_dual(repo)
    t, v = decs(tmp_path, dec_row("D-505", "work", f"demo:CODE-1@{dr.origin[:8]}"))
    p = build([item("CODE-1", kinds=("commit",))], t, v, dr,
              repos_dict={"demo": {"path": dr.path, "ref": "main"}})
    assert not [o for o in p.ops if o["op"] in ("close_on_decision", "add_evidence")]
    assert any(r["reason"] == "not on main" for r in p.d_in_review)


def test_gap_r7_nonexistent_configured_ref_stays_in_review(repo, tmp_path):
    # (c) configured ref that does not exist in the repo stays in_review
    t, v = decs(tmp_path, dec_row("D-505", "work", f"demo:CODE-1@{repo.merged[:8]}"))
    p = build([item("CODE-1", kinds=("commit",))], t, v, repo,
              repos_dict={"demo": {"path": repo.path, "ref": "origin/main"}})
    assert not [o for o in p.ops if o["op"] in ("close_on_decision", "add_evidence")]
    assert any(r["reason"] == "not on origin/main" for r in p.d_in_review)


def test_gap_r7_git_error_fails_closed_to_in_review(repo, tmp_path, monkeypatch):
    from plangraph.keeper.evidence_git import GitAdapter

    def boom(*args, **kwargs):
        raise RuntimeError("git execution failed")

    monkeypatch.setattr(GitAdapter, "is_ancestor", boom)
    text, view = decs(tmp_path,
                      dec_row("D-505", "work", f"demo:CODE-1@{repo.merged[:8]}"))
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)
    assert len([o for o in p.ops if o["op"] == "close_on_decision"]) == 0
    assert any(r["key"] == "demo:CODE-1" and "not on main" in r["reason"]
               for r in p.d_in_review)


def test_gap_r7_models_note_not_planned_for_claimed_or_in_review_item(repo, tmp_path):
    text, view = decs(tmp_path,
                      dec_row("D-505", "work Models: modelA",
                              f"demo:CODE-1@{repo.unmerged[:8]}"))
    # item already in_review
    p_in_rev = build([item("CODE-1", state="in_review", kinds=("commit",))],
                     text, view, repo)
    assert not any(o["op"] == "edit" and "note" in o.get("fields", {})
                   for o in p_in_rev.ops)
    out = io.StringIO()
    md_sync.report(p_in_rev, out, dry=True)
    report_text = out.getvalue()
    assert "in_review demo:CODE-1: D-505" in report_text

    # item already claimed
    p_claimed = build([item("CODE-1", state="claimed", kinds=("commit",))],
                      text, view, repo)
    assert not any(o["op"] == "edit" and "note" in o.get("fields", {})
                   for o in p_claimed.ops)
    out2 = io.StringIO()
    md_sync.report(p_claimed, out2, dry=True)
    assert "in_review demo:CODE-1: D-505" in out2.getvalue()


def test_gap_r7_readding_already_present_decision_evidence_not_planned(repo, tmp_path):
    text, view = decs(tmp_path,
                      dec_row("D-505", "work", f"demo:CODE-1@{repo.merged[:8]}"))
    # item already has evidence for D-505
    it = item("CODE-1", kinds=("commit",),
              evidence=[{"kind": "decision", "ref": {"id": "D-505"}}])
    p = build([it], text, view, repo)
    dec_ops = [o for o in p.ops if o["op"] == "add_evidence" and o.get("kind") == "decision"]
    assert len(dec_ops) == 0


def test_gap_r7_summary_field_order(repo, tmp_path):
    text_with, view_with = decs(
        tmp_path,
        dec_row("D-505", "one", f"demo:CODE-1@{repo.merged[:8]}")
    )
    p_with = build([item("CODE-1", kinds=("commit",))], text_with, view_with, repo)
    out_with = io.StringIO()
    md_sync.report(p_with, out_with, dry=True)
    summary_with = [line for line in out_with.getvalue().splitlines()
                    if line.startswith("md-sync: ")][-1]

    # Without D-rows
    p_without = plan.build_plan([], [], [], set(),
                                [item("CODE-1", kinds=("commit",))], None)
    out_without = io.StringIO()
    md_sync.report(p_without, out_without, dry=True)
    summary_without = [line for line in out_without.getvalue().splitlines()
                       if line.startswith("md-sync: ")][-1]

    import re
    keys_with = [m.split()[0] for m in summary_with.replace("md-sync: ", "").split(", ")]
    keys_without = [m.split()[0] for m in summary_without.replace("md-sync: ", "").split(", ")]
    assert keys_with == keys_without
    assert keys_with[-3:] == ["d-closes", "in-review", "warnings"]
    assert summary_with.endswith(", d-closes 1, in-review 0, warnings 0")


def test_gap_r8_evidence_kinds_any_plain_key_answers_behavior(repo, tmp_path):
    # Plain key (no @sha) on evidence_kinds: ["any"] takes the ANSWERS-path behaviour
    text, view = decs(tmp_path, dec_row("D-505", "work", "demo:ANY-1"))
    it = item("ANY-1", kinds=("any",))
    p = build([it], text, view, repo)
    # ANSWERS-path behavior: closes on decision
    close_op = [o for o in p.ops if o["op"] == "close_on_decision"]
    assert len(close_op) == 1
    assert p.d_closes == [{"key": "demo:ANY-1", "decision": "D-505"}]
    # Edits review_tier to 'none' and evidence_kinds to ['decision']
    edit_op = [o for o in p.ops if o["op"] == "edit"]
    assert len(edit_op) == 1
    assert edit_op[0]["fields"]["review_tier"] == "none"
    assert edit_op[0]["fields"]["evidence_kinds"] == ["decision"]


def test_gap_r8_evidence_kinds_any_unmerged_sha_in_review(repo, tmp_path):
    text, view = decs(tmp_path,
                      dec_row("D-505", "work", f"demo:ANY-1@{repo.unmerged[:8]}"))
    it = item("ANY-1", kinds=("any",))
    p = build([it], text, view, repo)
    # code-item treatment: unmerged sha => in_review, never closes
    assert len([o for o in p.ops if o["op"] == "close_on_decision"]) == 0
    assert any(r["key"] == "demo:ANY-1" and "not on main" in r["reason"]
               for r in p.d_in_review)


def test_gap_r8_evidence_kinds_any_merged_sha_closes(repo, tmp_path):
    text, view = decs(tmp_path,
                      dec_row("D-505", "work", f"demo:ANY-1@{repo.merged[:8]}"))
    it = item("ANY-1", kinds=("any",))
    p = build([it], text, view, repo)
    # merged sha => closes with commit evidence
    assert len([o for o in p.ops if o["op"] == "close_on_decision"]) == 1
    commit_ev = [o for o in p.ops if o["op"] == "add_evidence" and o.get("kind") == "commit"]
    assert len(commit_ev) == 1
    assert commit_ev[0]["ref"]["sha"] == repo.merged












