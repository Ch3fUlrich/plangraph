"""md_sync v1: parsing QUESTIONS/ANSWERS/decisions and redaction (synthetic fixtures)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import md_sync_parse as mp  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "md_sync"


def questions():
    return mp.parse_questions((FIX / "sample_questions.md").read_text(encoding="utf-8"))


def by_id(entries):
    return {e.id: e for e in entries}


def test_questions_ids_and_order():
    ids = [e.id for e in questions()]
    assert ids == ["Q-ALPHA", "Q-BETA-2", "Q-SHARED", "Q-GHOST",
                   "OS-1", "OS-2", "OS-3"]


def test_heading_with_operator_makes_op_step():
    e = by_id(questions())
    assert e["OS-1"].kind == "op" and e["OS-1"].title.startswith("OP: ")
    assert e["OS-3"].kind == "op"  # "Operator notes (misc)" contains Operator
    assert e["Q-ALPHA"].kind == "question"
    assert e["Q-ALPHA"].title.startswith("Q: ")


def test_title_is_first_bold_span():
    e = by_id(questions())
    assert e["Q-ALPHA"].title == "Q: Pick a storage backend"
    assert e["OS-1"].title == "OP: Rotate the demo credential"


def test_title_falls_back_to_first_sentence():
    e = by_id(questions())
    assert e["Q-BETA-2"].title == "Q: Should the gizmo retry on timeout?"
    assert e["OS-2"].title == "OP: Enable the nightly job for the sample service."


def test_description_is_rest_plus_sub_bullets():
    e = by_id(questions())
    assert e["Q-ALPHA"].description == (
        "for the widget cache. Needs a call before the freeze.\n"
        "- sub-bullet detail one\n- sub-bullet detail two")
    assert e["Q-BETA-2"].description == "Default is no. More text follows here."
    assert e["OS-3"].description == ""


def test_long_first_sentence_is_cut_at_120():
    text = "## Q\n- Q-LONG " + "word " * 60 + "end.\n"
    (entry,) = mp.parse_questions(text)
    assert len(entry.title) <= len("Q: ") + 120
    assert entry.title.startswith("Q: word word")


def test_non_entry_bullets_and_paragraphs_are_ignored():
    text = "## Q\n- plain bullet\n  - nested\n- Q-X1 **real**\npara line\n  not mine\n"
    (entry,) = mp.parse_questions(text)
    assert entry.id == "Q-X1" and entry.description == ""


def test_answers_entries_and_skipped():
    answers, skipped = mp.parse_answers((FIX / "sample_answers.md").read_text(encoding="utf-8"))
    got = {a.id: (a.decisions, a.date) for a in answers}
    assert got["Q-ALPHA"] == (["D-10"], "2026-09-30")  # notes section ignored
    assert got["OS-1"] == (["D-11", "D-12"], "2026-09-30")
    assert got["Q-GHOST"] == (["D-999"], "2026-10-01")
    assert "Q-BETA-2" not in got
    assert [s.id for s in skipped] == ["Q-BETA-2"]


MULTI = ("## 2026-10-02\n"
         "- OS-11 + OS-12 done (some text), OS-13 done, OS-14 done (text), "
         "OS-15/OS-16 resolved by something (D-901).\n")


def test_joined_ids_on_one_line_all_take_the_lines_last_d_group():
    answers, skipped = mp.parse_answers(MULTI)
    assert {a.id: a.decisions for a in answers} == {
        i: ["D-901"] for i in ("OS-11", "OS-12", "OS-13", "OS-14", "OS-15", "OS-16")}
    assert skipped == []


def test_each_sentence_takes_its_own_d_ids_and_a_replacement_is_not_an_answer():
    answers, skipped = mp.parse_answers(
        "## 2026-10-02\n"
        "- OS-21 obsolete: text (D-902). OS-22 superseded by OS-23 (text, D-903).\n")
    assert {a.id: a.decisions for a in answers} == {
        "OS-21": ["D-902"], "OS-22": ["D-903"]}
    assert skipped == []   # OS-23 is only the replacement named after "by"


def test_slash_separates_d_ids_like_a_comma():
    answers, skipped = mp.parse_answers(
        "## 2026-10-02\n- OS-31 done: text (D-904/D-905).\n")
    assert [(a.id, a.decisions) for a in answers] == [("OS-31", ["D-904", "D-905"])]
    assert skipped == []


def test_ids_after_by_or_in_parentheses_are_references_not_answers():
    answers, skipped = mp.parse_answers(
        "## 2026-10-02\n"
        "- OS-91 superseded by OS-92 (see OS-93), reviewed (D-910).\n")
    assert [(a.id, a.decisions) for a in answers] == [("OS-91", ["D-910"])]
    assert skipped == []


def test_joined_id_without_a_status_word_is_not_an_answer():
    answers, skipped = mp.parse_answers(
        "## 2026-10-02\n- OS-94 done with the details, OS-95 note (D-911).\n")
    assert {a.id: a.decisions for a in answers} == {"OS-94": ["D-911"]}
    assert skipped == []   # OS-95 starts a clause but has no status word


def test_decision_ids_normalised():
    ids = mp.parse_decisions((FIX / "decisions-log.md").read_text(encoding="utf-8"))
    assert ids == {"D-10", "D-11", "D-12"}


def test_decisions_ignores_fenced_and_indented_rows():
    text = "```\n| D-5 | x |\n```\n    | D-6 | x |\n| D-7 | p | d | b | w |\n"
    assert mp.parse_decisions(text) == {"D-7"}


def test_redaction_masks_and_counts():
    out, n = mp.redact("old token: Zx81kQ92LmPq77Vb and key AbCd1234Efgh here")
    assert "Zx81kQ92LmPq77Vb" not in out and "AbCd1234Efgh" not in out
    assert out.count("[redacted]") == 2 and n == 2


def test_redaction_masks_long_runs():
    hexrun = "a1" * 20
    # synthetic, not a credential
    b64 = ("QUJDREVGR0hJSktMTU" + "5PUFFSU1RVVldYWVo0" + "MTIzNDU2Nzg5")
    out, n = mp.redact(f"hash {hexrun} blob {b64}")
    assert hexrun not in out and b64 not in out and n == 2


def test_redaction_leaves_prose():
    text = "key management and token rotation, prefix rules"
    assert mp.redact(text) == (text, 0)


def test_text_after_a_truncated_title_is_kept_in_the_description():
    text = "## Q\n- Q-LONG " + "word " * 60 + "end.\n"
    (entry,) = mp.parse_questions(text)
    assert "end." in entry.description
    assert entry.title[len("Q: "):] + " " + entry.description == ("word " * 60 + "end.")
