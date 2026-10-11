"""qa-p2 W2: qa_backfill proposals (hermetic, synthetic fixtures)."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = str(ROOT / "tools" / "qa_backfill.py")
sys.path.insert(0, str(ROOT / "tools"))
import md_sync_parse as mp  # noqa: E402

HDR = "| Prio | ID | why | step | where |"
HDR_KP = "| Prio | ID | Kind | Parent | why | step | where |"


def run(qpath, outdir):
    return subprocess.run(
        [sys.executable, TOOL, "--questions", str(qpath), "--out", str(outdir)],
        capture_output=True, text=True)


def write_q(tmp_path, body, name="QUESTIONS.md"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def table(rows, header=HDR):
    return header + "\n|---|---|---|---|---|\n" + "\n".join(rows) + "\n" if rows else header + "\n|---|---|---|---|---|\n"


def read_out(outdir):
    prop = (outdir / "proposed-rows.md").read_text(encoding="utf-8")
    rev = (outdir / "review.md").read_text(encoding="utf-8")
    return prop, rev


def test_enumeration_abc(tmp_path):
    step = "Pick one a) enable mail; b) keep disabled; c) ask later"
    q = write_q(tmp_path, table([f"| 1 | Q-ENUM | why here | {step} | host |"]))
    out = tmp_path / "out"
    r = run(q, out)
    assert r.returncode == 0, r.stderr
    prop, rev = read_out(out)
    entries = mp.parse_questions(prop)
    assert len(entries) == 1 and entries[0].choices is not None
    assert len(entries[0].choices["options"]) == 3


def test_or_split(tmp_path):
    step = "Type approve in the box or apply the patch manually"
    q = write_q(tmp_path, table([f"| 1 | Q-OR | why | {step} | host |"]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, _ = read_out(out)
    (e,) = mp.parse_questions(prop)
    assert e.choices is not None and len(e.choices["options"]) == 2


def test_default_alternative_sentences(tmp_path):
    step = "Do the thing. Default: keep disabled. Alternative: enable mail now"
    q = write_q(tmp_path, table([f"| 1 | Q-DA | why | {step} | host |"]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, rev = read_out(out)
    (e,) = mp.parse_questions(prop)
    assert e.choices is not None and len(e.choices["options"]) >= 2
    assert e.choices["default"] is not None
    assert "default guessed" not in rev.lower() or "Q-DA" in rev


def test_deadline_date_vs_none(tmp_path):
    q = write_q(tmp_path, table([
        "| 1 | Q-DL | need by 2026-11-02 | do it | host |",
        "| 1 | Q-DN | why | do it Deadline: none | host |",
    ]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, _ = read_out(out)
    by = {e.id: e for e in mp.parse_questions(prop)}
    assert by["Q-DL"].choices["deadline"] == "2026-11-02"
    assert by["Q-DN"].choices["deadline"] is None


def test_generic_fallback_flag(tmp_path):
    q = write_q(tmp_path, table(["| 1 | Q-GEN | why | just do the thing | host |"]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, rev = read_out(out)
    (e,) = mp.parse_questions(prop)
    assert e.choices is not None and len(e.choices["options"]) == 2
    assert "generic" in rev


def test_valid_options_skipped(tmp_path):
    step = "do it Options: enable = x; *stay = y"
    q = write_q(tmp_path, table([f"| 1 | Q-HAS | why | {step} | host |"]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, rev = read_out(out)
    assert "already has options: Q-HAS" in rev
    assert "Q-HAS" not in prop or "Options:" not in prop.split("Q-HAS", 1)[1][:200] or prop.count("Q-HAS") == 0


def test_malformed_flagged_and_replaced(tmp_path):
    bad = "do it Options: a = x; ; b = y"
    q = write_q(tmp_path, table([f"| 1 | Q-BAD | why | {bad} | host |"]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, rev = read_out(out)
    assert "malformed block replaced" in rev
    (e,) = mp.parse_questions(prop)
    assert e.choices is not None
    assert " ; ; " not in e.description


def test_parent_token_and_cell_carried_not_invented(tmp_path):
    q = write_q(tmp_path, table([
        "| 1 | Q-PT | why | do it Parent: PH-OPERATOR | host |",
        "| 1 | Q-NP | why | just do it | host |",
    ]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, _ = read_out(out)
    by = {e.id: e for e in mp.parse_questions(prop)}
    assert by["Q-PT"].parent == "PH-OPERATOR"
    assert by["Q-NP"].parent is None
    # parent column variant
    hdr = "| Prio | ID | Parent | why | step | where |\n|---|---|---|---|---|---|\n"
    q2 = write_q(tmp_path, hdr + "| 1 | Q-PC | routing:G1 | why | do it | host |\n", name="Q2.md")
    out2 = tmp_path / "out2"
    assert run(q2, out2).returncode == 0
    prop2, _ = read_out(out2)
    assert "routing:G1" in prop2
    (e2,) = mp.parse_questions(prop2)
    assert e2.parent == "routing:G1"


def test_kind_parent_columns_preserved(tmp_path):
    hdr = HDR_KP + "\n|---|---|---|---|---|---|---|\n"
    body = hdr + "| 1 | Q-KP | TASK | routing:G1 | why here | pick a) x; b) y | host |\n"
    q = write_q(tmp_path, body)
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, _ = read_out(out)
    assert prop.splitlines()[0].strip() == HDR_KP
    (e,) = mp.parse_questions(prop)
    assert e.choices is not None and e.parent == "routing:G1" and e.wait == "task"


def test_secret_masked_both_files(tmp_path):
    secret = "a1" * 20  # bare 40-hex run: redact() masks it, gitleaks stays quiet
    q = write_q(tmp_path, table([f"| 1 | Q-SEC | why here | do it with hash {secret} | host |"]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, rev = read_out(out)
    assert secret not in prop and secret not in rev
    assert "[redacted]" in prop and "[redacted]" in rev


def test_round_trip_all_proposed_parse(tmp_path):
    q = write_q(tmp_path, table([
        "| 1 | Q-R1 | why | pick a) x; b) y | host |",
        "| 1 | Q-R2 | why | just do it | host |",
        "| 1 | Q-R3 | by 2026-12-01 | type ok or apply fix | host |",
    ]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, _ = read_out(out)
    for e in mp.parse_questions(prop):
        assert e.choices is not None and len(e.choices["options"]) >= 2


def test_determinism(tmp_path):
    body = table(["| 1 | Q-D1 | why | pick a) x; b) y | host |"])
    q = write_q(tmp_path, body)
    o1, o2 = tmp_path / "o1", tmp_path / "o2"
    assert run(q, o1).returncode == 0
    assert run(q, o2).returncode == 0
    assert (o1 / "proposed-rows.md").read_bytes() == (o2 / "proposed-rows.md").read_bytes()
    assert (o1 / "review.md").read_bytes() == (o2 / "review.md").read_bytes()


def test_writes_only_into_dir(tmp_path):
    q = write_q(tmp_path, table(["| 1 | Q-W | why | do it | host |"]))
    qbytes = q.read_bytes()
    before = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    out = tmp_path / "d" / "nested"
    assert run(q, out).returncode == 0
    assert q.read_bytes() == qbytes
    after = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    new = set(after) - set(before)
    assert new == {"d/nested/proposed-rows.md", "d/nested/review.md"}
    for k in before:
        assert after[k] == before[k]


def test_refuses_out_containing_questions(tmp_path):
    q = write_q(tmp_path, table(["| 1 | Q-X | why | do it | host |"]))
    r = subprocess.run([sys.executable, TOOL, "--questions", str(q), "--out", str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == 2


def test_missing_questions_rc2(tmp_path):
    r = subprocess.run([sys.executable, TOOL, "--questions", str(tmp_path / "nope.md"),
                        "--out", str(tmp_path / "o")], capture_output=True, text=True)
    assert r.returncode == 2


def test_header_counts_line(tmp_path):
    step_ok = "do it Options: a = x; *b = y"
    q = write_q(tmp_path, table([
        f"| 1 | Q-A | why | {step_ok} | host |",
        "| 1 | Q-B | why | just do it | host |",
    ]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    _, rev = read_out(out)
    assert "rows 2, proposed 1, already-has-options 1, could-not-derive 0" in rev
    assert "accept: replace the row with the proposed one" in rev
