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
    assert "salvaged malformed block" in rev
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


# --- W3: salvage, clean labels, generic wording, default mapping, properties ---

def test_salvage_long_label_and_enumerator(tmp_path):
    long_label = "enable the mail handling for the whole team today please add extra words to be long"
    assert len(long_label) > 60
    step = f"do it Options: a) {long_label}; *b) keep disabled = tell owner"
    q = write_q(tmp_path, table([f"| 1 | Q-S1 | why | {step} | host |"]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, rev = read_out(out)
    assert "salvaged malformed block" in rev
    (e,) = mp.parse_questions(prop)
    assert e.choices is not None and len(e.choices["options"]) == 2
    labels = [o["label"] for o in e.choices["options"]]
    assert all(len(l) <= 60 for l in labels)
    assert all("a)" not in l for l in labels)
    # long original moved into a consequence, default stays on starred option
    assert any("consequence" in o for o in e.choices["options"])
    default_label = next(o["label"] for o in e.choices["options"] if o["id"] == e.choices["default"])
    assert default_label == "keep disabled"


def test_salvage_paren_enumerator_and_deadline(tmp_path):
    long_label = "first choice here with many extra words added to push it past sixty characters total"
    assert len(long_label) > 60
    step = f"do it Options: (a) {long_label}; (b) second choice there Deadline: 2026-12-01"
    q = write_q(tmp_path, table([f"| 1 | Q-S2 | why here | {step} | host |"]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, rev = read_out(out)
    assert "salvaged malformed block" in rev
    (e,) = mp.parse_questions(prop)
    assert e.choices is not None
    assert e.choices["deadline"] == "2026-12-01"
    labels = [o["label"] for o in e.choices["options"]]
    assert "second choice there" in labels


def test_salvage_keeps_up_to_8(tmp_path):
    long_label = "first choice with plenty of extra words to make the label exceed sixty characters"
    assert len(long_label) > 60
    parts = [f"a) {long_label}"] + [f"choice number {i}" for i in range(2, 7)]
    step = "do it Options: " + "; ".join(parts)
    q = write_q(tmp_path, table([f"| 1 | Q-S3 | why | {step} | host |"]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, rev = read_out(out)
    assert "salvaged malformed block" in rev
    (e,) = mp.parse_questions(prop)
    assert e.choices is not None and len(e.choices["options"]) == 6


def test_salvage_failure_is_could_not_derive_not_generic(tmp_path):
    # empty labels cannot be salvaged: must NOT fall back to generic pair
    step = "do it Options: = ; = "
    q = write_q(tmp_path, table([f"| 1 | Q-SF | why | {step} | host |"]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, rev = read_out(out)
    assert "could not derive" in rev and "Q-SF" in rev
    assert "do it as written" not in prop
    assert "rows 1, proposed 0, already-has-options 0, could-not-derive 1" in rev


def test_garbled_or_split_uses_generic(tmp_path):
    step = "In the session add the allow rule `Bash(cd or say hello"
    q = write_q(tmp_path, table([f"| 1 | Q-G2 | why | {step} | host |"]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, rev = read_out(out)
    assert "generic" in rev
    (e,) = mp.parse_questions(prop)
    labels = [o["label"] for o in e.choices["options"]]
    assert labels == ["do it as written", "skip or defer"]
    assert all("`" not in l for l in labels)


def test_garbled_enumeration_falls_back_to_generic(tmp_path):
    step = "Pick one a) `run this; b) **bold thing"
    q = write_q(tmp_path, table([f"| 1 | Q-G3 | why | {step} | host |"]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, rev = read_out(out)
    assert "generic" in rev
    (e,) = mp.parse_questions(prop)
    for o in e.choices["options"]:
        assert "`" not in o["label"] and "*" not in o["label"]


def test_generic_pair_wording(tmp_path):
    q = write_q(tmp_path, table(["| 1 | Q-GW | why | just do the thing | host |"]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, _ = read_out(out)
    (e,) = mp.parse_questions(prop)
    labels = [o["label"] for o in e.choices["options"]]
    assert labels == ["do it as written", "skip or defer"]
    for o in e.choices["options"]:
        assert o.get("consequence") == "tell " + "routing-" + "00"
    assert e.choices["default"] == e.choices["options"][0]["id"]


def test_default_named_maps_and_unmatched_flags_guessed(tmp_path):
    q = write_q(tmp_path, table([
        "| 1 | Q-M1 | Default: keep disabled | pick a) enable mail; b) keep disabled | host |",
        "| 1 | Q-M2 | Default: unknown thing here | pick a) enable mail; b) keep disabled | host |",
    ]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, rev = read_out(out)
    by = {e.id: e for e in mp.parse_questions(prop)}
    lab1 = next(o["label"] for o in by["Q-M1"].choices["options"] if o["id"] == by["Q-M1"].choices["default"])
    assert lab1 == "keep disabled"
    assert rev.count("default guessed: first") == 1


def test_when_convenient_maps_or_flags(tmp_path):
    q = write_q(tmp_path, table([
        "| 1 | Q-WC | why here | pick a) do it when convenient; b) skip for now | host |",
    ]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, _ = read_out(out)
    (e,) = mp.parse_questions(prop)
    lab = next(o["label"] for o in e.choices["options"] if o["id"] == e.choices["default"])
    assert "when convenient" in lab.lower()


def test_source_has_no_cyrillic_or_dead_code(tmp_path):
    src = (ROOT / "tools" / "qa_backfill.py").read_text(encoding="utf-8")
    assert all(ord(c) < 128 for c in src)
    assert "_TRIG =" not in src and "_TRIG=" not in src and "HDR_DEFAULT" not in src
    assert "orig_ids" not in src and "kind-ва" not in src


def _balanced(s):
    stack = []
    pairs = {")": "(", "]": "["}
    for c in s:
        if c in "([":
            stack.append(c)
        elif c in ") ]".replace(" ", ""):
            if not stack or stack.pop() != pairs[c]:
                return False
    return not stack


def _n_cells(line):
    parts = line.strip().split("|")[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return len([p.strip() for p in parts])


def test_opt_order_columns_follow_header(tmp_path):
    cases = [
        ("| Prio | ID | Parent | Kind | why | step | where |",
         "| 1 | Q-O1 | routing:G1 | TASK | why here | pick a) x; b) y | host |"),
        ("| Prio | ID | Kind | Parent | why | step | where |",
         "| 1 | Q-O2 | TASK | routing:G1 | why here | pick a) x; b) y | host |"),
        ("| Prio | ID | Kind | why | step | where |",
         "| 1 | Q-O3 | TASK | why here | pick a) x; b) y | host |"),
        ("| Prio | ID | Parent | why | step | where |",
         "| 1 | Q-O4 | routing:G1 | why here | pick a) x; b) y | host |"),
        ("| Prio | ID | why | step | where |",
         "| 1 | Q-O5 | why here | pick a) x; b) y | host |"),
    ]
    for i, (header, row) in enumerate(cases):
        n = _n_cells(header)
        sep = "|" + "|".join(["---"] * n) + "|"
        body = header + "\n" + sep + "\n" + row + "\n"
        want = mp.parse_questions(body)
        assert len(want) == 1
        q = write_q(tmp_path, body, name=f"QO{i}.md")
        out = tmp_path / f"out{i}"
        assert run(q, out).returncode == 0
        prop, _ = read_out(out)
        lines = [l for l in prop.splitlines() if l.strip().startswith("|")]
        assert len(lines) >= 2
        assert _n_cells(lines[0]) == n
        assert _n_cells(lines[2]) == n
        (got,) = mp.parse_questions(prop)
        assert got.wait == want[0].wait, (header, got.wait, want[0].wait)
        assert got.parent == want[0].parent, (header, got.parent, want[0].parent)


def test_incidental_ph_token_not_parent(tmp_path):
    q = write_q(tmp_path, table(["| 1 | Q-PH | reuse the PH-AUDIT conventions | just do the thing | host |"]))
    out = tmp_path / "out"
    assert run(q, out).returncode == 0
    prop, rev = read_out(out)
    assert "parent: none" in rev
    assert "Parent:" not in prop
    assert "PH-AUDIT" in prop
    (e,) = mp.parse_questions(prop)
    assert e.parent is None


def test_properties_over_10_rows(tmp_path):
    rows = [
        "| 1 | Q-P01 | why | pick a) enable mail; b) keep disabled | host |",
        "| 1 | Q-P02 | why | do it Options: a) a very long label that surely exceeds sixty characters total here yes; *b) short = fine | host |",
        "| 1 | Q-P03 | why | do it Options: (a) first choice here; (b) second choice there | host |",
        "| 1 | Q-P04 | why | just do the thing | host |",
        "| 1 | Q-P05 | why | type approve in the box or apply the patch manually | host |",
        "| 1 | Q-P06 | why | In the session add the allow rule `Bash(cd or say hello | host |",
        "| 1 | Q-P07 | why | Do the thing. Default: keep disabled. Alternative: enable mail now | host |",
        "| 1 | Q-P08 | need by 2026-11-02 | do it | host |",
        "| 1 | Q-P09 | why | do it Parent: PH-OPERATOR | host |",
        "| 1 | Q-P10 | why | pick a) one; b) two; c) three; d) four; e) five | host |",
        "| 1 | Q-P11 | why | pick a) enable mail; b) keep disabled when convenient | host |",
    ]
    q = write_q(tmp_path, table(rows))
    o1, o2 = tmp_path / "o1", tmp_path / "o2"
    assert run(q, o1).returncode == 0
    assert run(q, o2).returncode == 0
    assert (o1 / "proposed-rows.md").read_bytes() == (o2 / "proposed-rows.md").read_bytes()
    prop = (o1 / "proposed-rows.md").read_text(encoding="utf-8")
    dangling = {"and", "or", "the", "to", "in", "of"}
    for e in mp.parse_questions(prop):
        assert e.choices is not None and len(e.choices["options"]) >= 2
        for o in e.choices["options"]:
            for key in ("label", "consequence"):
                if key not in o:
                    continue
                v = o[key]
                assert "`" not in v and ";" not in v and "|" not in v
                assert not v.lstrip().startswith("*")
                assert _balanced(v)
            last = o["label"].strip(" .;:-,!?\"'").split()[-1].lower()
            assert last not in dangling
            assert len(o["label"]) <= 60
