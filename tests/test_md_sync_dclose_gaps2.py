"""Gap tests part 2 for md_sync router D-row pass (R5 models, status file)."""
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


@pytest.fixture
def repo(tmp_path):
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
          repos_dict=None):
    return plan.build_plan([], list(answers), [], ids or set(), items, view,
                           decisions_text=text,
                           repos=repos_dict if repos_dict is not None else repos(repo),
                           since=since)


def test_gap_r5_models_status_and_report_lines(repo, tmp_path):
    seg_merged = "model=test-merged"
    seg_review = "model=test-review"
    rows = (
        dec_row("D-505", f"work Models: {seg_merged}",
                f"demo:CODE-1@{repo.merged[:8]}")
        + dec_row("D-506", f"work Models: {seg_review}",
                  f"demo:CODE-2@{repo.unmerged[:8]}")
    )
    text, view = decs(tmp_path, rows)
    items = [item("CODE-1", kinds=("commit",)), item("CODE-2", kinds=("commit",))]
    p = build(items, text, view, repo)

    out = io.StringIO()
    md_sync.report(p, out, dry=True)
    report_text = out.getvalue()
    assert f"models demo:CODE-1: {seg_merged}" in report_text
    assert f"models demo:CODE-2: {seg_review}" in report_text

    st = md_sync.status_doc("dry")
    md_sync.fill_status(st, p)
    assert "models" in st
    assert {"key": "demo:CODE-1", "decision": "D-505", "text": seg_merged} in st["models"]
    assert {"key": "demo:CODE-2", "decision": "D-506", "text": seg_review} in st["models"]


def test_gap_r5_credential_shaped_segment_is_masked(repo, tmp_path):
    # synthetic, not a credential
    secret_tail = "commitx99"
    secret = "add/" + secret_tail
    hostile = "login see " + secret
    rows = dec_row("D-505", f"work Models: {hostile}",
                   f"demo:CODE-1@{repo.merged[:8]}")
    text, view = decs(tmp_path, rows)
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)

    # 1. Masked in note
    note = p.ops[0]["fields"]["note"]
    assert secret not in note and "[redacted]" in note

    # 2. Masked in report line
    out = io.StringIO()
    md_sync.report(p, out, dry=True)
    report_text = out.getvalue()
    assert secret not in report_text
    assert f"models demo:CODE-1: login see [redacted]" in report_text

    # 3. Masked in status entry
    st = md_sync.status_doc("dry")
    md_sync.fill_status(st, p)
    assert "models" in st
    assert any(m["key"] == "demo:CODE-1" and secret not in m["text"] and "[redacted]" in m["text"]
               for m in st["models"])


def test_gap_r5_no_models_line_for_conflicting_sha_row(repo, tmp_path):
    rows = (
        dec_row("D-505", "work Models: segA", f"demo:CODE-1@{repo.merged[:8]}")
        + dec_row("D-506", "work Models: segB", f"demo:CODE-1@{repo.unmerged[:8]}")
    )
    text, view = decs(tmp_path, rows)
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)

    out = io.StringIO()
    md_sync.report(p, out, dry=True)
    report_text = out.getvalue()
    assert "models demo:CODE-1" not in report_text

    st = md_sync.status_doc("dry")
    md_sync.fill_status(st, p)
    assert not any(m.get("key") == "demo:CODE-1" for m in st.get("models", []))


def test_gap_r5_not_in_graph_counted_in_close_skipped(repo, tmp_path):
    text, view = decs(tmp_path,
                      dec_row("D-505", "work", f"demo:GHOST@{repo.merged[:8]}"))
    p = build([item("CODE-1", kinds=("commit",))], text, view, repo)

    out = io.StringIO()
    md_sync.report(p, out, dry=True)
    report_text = out.getvalue()
    summary = [line for line in report_text.splitlines() if line.startswith("md-sync: ")][-1]
    # Summary field close-skipped counts the not-in-graph key
    assert "close-skipped 1" in summary

    st = md_sync.status_doc("dry")
    md_sync.fill_status(st, p)
    assert any(cs.get("key") == "demo:GHOST" for cs in st.get("close_skipped", []))


def test_gap_r7_status_file_fields_shape(repo, tmp_path):
    rows = (
        dec_row("D-505", "work Models: modelA", f"demo:CODE-1@{repo.merged[:8]}")
        + dec_row("D-506", "work Models: modelB", f"demo:CODE-2@{repo.unmerged[:8]}")
        + dec_row("D-507", "decide", f"demo:DEC-1@{repo.merged[:8]}")
    )
    text, view = decs(tmp_path, rows)
    items = [
        item("CODE-1", kinds=("commit",)),
        item("CODE-2", kinds=("commit",)),
        item("DEC-1", tier="none", kinds=("decision",)),
    ]
    p = build(items, text, view, repo)
    st = md_sync.status_doc("dry")
    md_sync.fill_status(st, p)

    # Check d_closes
    assert isinstance(st["d_closes"], list)
    assert any(c["key"] == "demo:CODE-1" and c["decision"] == "D-505"
               for c in st["d_closes"])
    for entry in st["d_closes"]:
        assert set(entry.keys()) == {"key", "decision"}

    # Check d_in_review
    assert isinstance(st["d_in_review"], list)
    assert any(r["key"] == "demo:CODE-2" and r["decision"] == "D-506"
               and r["sha"] == repo.unmerged[:8] and "not on main" in r["reason"]
               for r in st["d_in_review"])
    for entry in st["d_in_review"]:
        assert set(entry.keys()) == {"key", "decision", "sha", "reason"}

    # Check warnings
    assert isinstance(st["warnings"], list)
    assert any(w["key"] == "demo:DEC-1" and w["decision"] == "D-507"
               and "sha ignored" in w["text"]
               for w in st["warnings"])
    for entry in st["warnings"]:
        assert set(entry.keys()) == {"key", "decision", "text"}

    # Check models
    assert "models" in st
    assert isinstance(st["models"], list)
    assert any(m["key"] == "demo:CODE-1" and m["decision"] == "D-505"
               and m["text"] == "modelA"
               for m in st["models"])
    for entry in st["models"]:
        assert set(entry.keys()) == {"key", "decision", "text"}




