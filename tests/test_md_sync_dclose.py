"""md_sync D-505: router D-rows with ``Closes: key@sha`` gate closes on the merge.

Planning-level tests drive ``build_plan`` with a synthetic decisions log and a
real temp git repo (a merged commit on ``main``, an unmerged one on a side
branch); the apply-level test runs the planned ops through the keeper store's
``apply_ops`` in-process (the same verbs the router's batch applies).
"""
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import apply_batch as ab  # noqa: E402
import md_sync  # noqa: E402
import md_sync_dclose as dclose  # noqa: E402
import md_sync_parse as mp  # noqa: E402
import md_sync_plan as plan  # noqa: E402
from plangraph.keeper.evidence_decisions import DecisionsAdapter  # noqa: E402
from plangraph.keeper.evidence_git import GitAdapter, LegacyCommitVerifier  # noqa: E402

# synthetic, not a credential
SEG_A = "writer=ovh/Qwen3.5-397B; seats=ovh/gpt-oss-120b:pass"
SEG_B = "research=ovh/Qwen3.5-397B"


@pytest.fixture
def repo(tmp_path):
    """A temp git repo: one commit on main (merged), one on a side branch."""
    path = tmp_path / "repo"
    path.mkdir()

    def run(*args):
        out = subprocess.run(["git", *args], cwd=path, capture_output=True,
                             text=True)
        assert out.returncode == 0, out.stderr
        return out

    run("init", "-q", "-b", "main")
    run("config", "user.email", "synthetic@example.invalid")
    run("config", "user.name", "synthetic")
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
          repos=None):
    return plan.build_plan([], list(answers), [], ids or set(), items, view,
                           decisions_text=text, repos=repos if repos is not None
                           else repos_map(repo), since=since)


def repos_map(repo):
    return repos(repo)


def assert_ops_valid(ops):
    """Every planned op must hold the shapes apply_batch (and the keeper) accept."""
    for op in ops:
        ab.validate_op(op)
        ab.build_request(op, "ANSWERS.md", "0" * 64)


def close_ops(ops):
    return [o for o in ops if o["op"] == "close_on_decision"]


def decision_refs(ops):
    return [o["ref"]["id"] for o in ops if o["op"] == "add_evidence"
            and o["kind"] == "decision"]


def commit_ref(ops):
    out = [o for o in ops if o["op"] == "add_evidence" and o["kind"] == "commit"]
    assert len(out) == 1
    return out[0]


# ------------------------------------------------------------- branch D: sha

def test_merged_code_item_closes_with_full_commit_evidence(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-505", "ship code one",
                                        f"demo:CODE-1@{repo.merged[:8]}"))
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)
    assert [o["op"] for o in p.ops] == [
        "edit", "legacy_state", "add_evidence", "add_evidence",
        "close_on_decision"]
    assert p.ops[0]["fields"] == {"review_tier": "none",
                                  "evidence_kinds": ["decision", "commit"]}
    assert p.ops[1]["note"] == (f"router D-505: Closes demo:CODE-1@"
                                f"{repo.merged[:8]}")
    assert decision_refs(p.ops) == ["D-505"]
    commit = commit_ref(p.ops)
    # abbreviated token resolves to the full 40-hex id
    assert commit["ref"]["sha"] == repo.merged
    assert commit["repo"] == "demo"
    assert repo.path not in json.dumps(p.ops)
    out = io.StringIO()
    md_sync.report(p, out, dry=True)
    assert repo.path not in out.getvalue()
    status = md_sync.status_doc("dry")
    md_sync.fill_status(status, p)
    assert repo.path not in json.dumps(status)
    assert p.d_closes == [{"key": "demo:CODE-1", "decision": "D-505"}]
    assert p.d_lines == ["close demo:CODE-1: by D-505"]
    assert_ops_valid(p.ops)


def test_uppercase_token_resolves_to_the_full_lowercase_id(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-505", "ship code one",
                                        f"demo:CODE-1@{repo.merged[:8].upper()}"))
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)
    assert commit_ref(p.ops)["ref"]["sha"] == repo.merged
    assert len(close_ops(p.ops)) == 1


def test_unmerged_commit_plans_in_review_only_not_a_close(repo, tmp_path):
    """Fails if the ancestor check were skipped: this commit exists but is not
    on main, so it must not close."""
    text, view = decs(tmp_path, dec_row("D-506", "ship code two",
                                        f"demo:CODE-2@{repo.unmerged[:8]}"))
    p = build([item("CODE-2", kinds=("commit",))], text, view, repo)
    assert [o["op"] for o in p.ops] == ["legacy_state"]
    assert p.ops[0]["state"] == "in_review"
    assert p.ops[0]["note"] == (f"router D-506: Closes demo:CODE-2@"
                                f"{repo.unmerged[:8]}; not on main")
    assert close_ops(p.ops) == []
    assert p.d_in_review == [{"key": "demo:CODE-2", "decision": "D-506",
                              "sha": repo.unmerged[:8], "reason": "not on main"}]
    assert p.d_lines == [f"in_review demo:CODE-2: D-506 @{repo.unmerged[:8]}"
                         " not on main"]
    assert_ops_valid(p.ops)


def test_unmerged_item_already_in_review_plans_nothing_but_is_listed(repo,
                                                                     tmp_path):
    text, view = decs(tmp_path, dec_row("D-506", "ship code two",
                                        f"demo:CODE-2@{repo.unmerged[:8]}"))
    p = build([item("CODE-2", state="in_review", kinds=("commit",))],
              text, view, repo)
    assert p.ops == []
    assert p.d_in_review and p.d_lines  # idempotent ops, still reported


def test_repo_not_registered_fails_closed_to_in_review(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-505", "ship code one",
                                        f"demo:CODE-1@{repo.merged[:8]}"))
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo,
              repos={})
    assert [o["op"] for o in p.ops] == ["legacy_state"]
    assert p.ops[0]["note"].endswith("; repo not registered")
    assert p.d_in_review[0]["reason"] == "repo not registered"
    assert close_ops(p.ops) == []


def test_git_error_bad_repo_path_fails_closed_to_in_review(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-505", "ship code one",
                                        f"demo:CODE-1@{repo.merged[:8]}"))
    bad = {"demo": {"path": str(tmp_path / "no-such-repo"), "ref": "main"}}
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo, repos=bad)
    assert [o["op"] for o in p.ops] == ["legacy_state"]
    assert p.ops[0]["note"].endswith("; not on main")
    assert p.d_in_review[0]["sha"] == repo.merged[:8]
    assert close_ops(p.ops) == []


def test_unknown_sha_token_fails_closed_to_in_review(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-505", "ship code one",
                                        "demo:CODE-1@deadbeef"))
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)
    assert [o["op"] for o in p.ops] == ["legacy_state"]
    assert "deadbeef; not on main" in p.ops[0]["note"]
    assert close_ops(p.ops) == []


# ------------------------------------------------------------- branch C

def test_decision_only_item_closes_on_decision_and_warns_about_sha(repo,
                                                                  tmp_path):
    text, view = decs(tmp_path, dec_row("D-505", "decided",
                                        f"demo:DEC-1@{repo.merged[:8]}"))
    p = build([item("DEC-1", tier="none", kinds=("decision",))],
              text, view, repo)
    assert [o["op"] for o in p.ops] == ["legacy_state", "add_evidence",
                                        "close_on_decision"]
    assert p.warnings == [{"key": "demo:DEC-1", "decision": "D-505",
                           "text": "sha ignored on decision-only item demo:DEC-1"}]
    assert p.d_closes == [{"key": "demo:DEC-1", "decision": "D-505"}]


def test_decision_only_item_without_sha_closes_without_warning(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-505", "decided", "demo:DEC-1"))
    p = build([item("DEC-1", tier="none", kinds=("decision",))],
              text, view, repo)
    assert len(close_ops(p.ops)) == 1 and p.warnings == []


# ------------------------------------------------------------- branch E

def test_code_item_named_without_sha_is_listed_in_review_never_closed(repo,
                                                                      tmp_path):
    text, view = decs(tmp_path, dec_row("D-508", "code later", "demo:CODE-3"))
    p = build([item("CODE-3", kinds=("commit",))], text, view, repo)
    assert [o["op"] for o in p.ops] == ["legacy_state"]
    assert p.ops[0]["note"] == "router D-508: Closes demo:CODE-3 (no sha)"
    assert close_ops(p.ops) == []
    assert p.d_in_review == [{"key": "demo:CODE-3", "decision": "D-508",
                              "sha": None, "reason": "no sha"}]
    assert p.d_lines == ["listed demo:CODE-3: D-508 no sha"]


def test_plain_key_already_in_review_plans_nothing_but_is_listed(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-508", "code later", "demo:CODE-3"))
    p = build([item("CODE-3", state="in_review", kinds=("commit",))],
              text, view, repo)
    assert p.ops == []
    assert p.d_in_review and p.d_lines


# ------------------------------------------------------- branches A, B, F

def test_key_not_in_graph_is_a_close_skipped_entry(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-509", "ghost",
                                        f"demo:GHOST@{repo.merged[:8]}"))
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)
    assert p.dclose_skipped == [("GHOST", "D-509", "demo:GHOST", "not in graph")]
    assert p.ops == []


def test_done_item_is_quiet(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-505", "ship code one",
                                        f"demo:CODE-1@{repo.merged[:8]}"))
    p = build([item("CODE-1", state="done", kinds=("commit",))],
              text, view, repo)
    assert p.ops == [] and p.d_closes == [] and p.d_in_review == []


def test_non_router_row_is_ignored(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-505", "ship code one",
                                        f"demo:CODE-1@{repo.merged[:8]}",
                                        by="operator"))
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)
    assert p.ops == [] and p.d_closes == [] and p.d_in_review == []


def test_fenced_row_is_ignored(repo, tmp_path):
    row = dec_row("D-505", "ship code one", f"demo:CODE-1@{repo.merged[:8]}")
    text, view = decs(tmp_path, "```\n" + row + "```\n")
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)
    assert p.ops == []


def test_decisions_since_ignores_lower_rows(repo, tmp_path):
    text, view = decs(tmp_path,
                      dec_row("D-505", "ship code one",
                              f"demo:CODE-1@{repo.merged[:8]}"),
                      dec_row("D-511", "ship code three", "demo:CODE-3"))
    p = build([item("CODE-1", kinds=("commit",)),
               item("CODE-3", kinds=("commit",))], text, view, repo, since=506)
    assert all(o.get("key") != "demo:CODE-1" for o in p.ops)
    assert "listed demo:CODE-3: D-511 no sha" in p.d_lines


def test_invalid_closes_token_reports_one_line_no_ops(repo, tmp_path):
    text, view = decs(tmp_path, dec_row("D-512", "bad token",
                                        "demo:BAD@zzz"))
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)
    assert p.invalid_tokens == ["D-512"]
    assert p.ops == []


def test_malformed_closes_keyword_reports_invalid(repo, tmp_path):
    # L5 shape: the keyword exists but no valid project:ID@sha token follows.
    text, view = decs(tmp_path, dec_row("D-512", "bad token", "demo: BAD"))
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)
    assert p.invalid_tokens == ["D-512"] and p.ops == []


def test_conflicting_shas_across_rows_are_not_planned(repo, tmp_path):
    text, view = decs(tmp_path,
                      dec_row("D-513", "one", f"demo:CF@{repo.merged[:8]}"),
                      dec_row("D-514", "two", f"demo:CF@{repo.unmerged[:8]}"))
    p = build([item("CF", kinds=("commit",))], text, view, repo)
    assert p.ops == [] and close_ops(p.ops) == []
    assert "D-514" in p.invalid_tokens


def test_prefix_compatible_shas_use_the_last_rows_sha(repo, tmp_path):
    text, view = decs(tmp_path,
                      dec_row("D-513", "one", f"demo:PF@{repo.merged[:8]}"),
                      dec_row("D-514", "two", f"demo:PF@{repo.merged[:10]}"))
    p = build([item("PF", kinds=("commit",))], text, view, repo)
    assert commit_ref(p.ops)["ref"]["sha"] == repo.merged
    assert p.d_closes == [{"key": "demo:PF", "decision": "D-514"}]
    assert sorted(decision_refs(p.ops)) == ["D-513", "D-514"]


