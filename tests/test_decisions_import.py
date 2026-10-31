"""tools/decisions_import.py: rules markdown -> keeper batch of decision items.

Every string here is synthetic; no real text, names or paths appear.
"""
import hashlib
import io
import os
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import apply_batch  # noqa: E402
import decisions_import  # noqa: E402

FIXTURE = """# Synthetic rules fixture

## 1. Alpha section

1.1 **Base rule.** Hold the base text (D-100) `[keep]`

1.2 **Second rule.** Refers to rule 1.1 and rule 0.2a (D-101, D-102)
and continues here with extra words.
`[gate: path/verb]`

0.2a **Letter rule.** Variant text see 1.1 `[temporary]`

1.3 **Third rule.** Self ref rule 1.3 plus rules 1.1/1.2 (D-103)
`[keep; gate-todo GT3]`

## 2. Beta section

2.1 **Fourth rule.** Cross section see 1.1. `[keep]`

2.2 **Fifth rule.** Per 0.2a and rules 1.2/2.1 (D-104) `[temporary]`

2.3 **Sixth rule.** Plain text without refs (D-105, D-100) `[keep]`
"""

CYCLE = "## 1. Cycle section\n\n1.1 **First rule.** See 1.2. `[keep]`\n\n1.2 **Second rule.** See 1.1. `[keep]`\n"
UNKNOWN = "## 1. Ref section\n\n1.1 **Odd rule.** See 9.9 and rule 1.2. `[keep]`\n\n1.2 **Even rule.** Plain text. `[keep]`\n"
DUP = "## 1. Dup section\n\n1.1 **One rule.** Text here. `[keep]`\n\n1.1 **Two rule.** Text here. `[keep]`\n"
NOTITLE = "## 1. Title section\n\n1.1 **Good rule.** Text here. `[keep]`\n\n1.2 Missing bold title here. `[keep]`\n"

NOSEMI = "x " * 80 + "tail"
LONGSENT = "w " * 80 + "tail."
PLAIN = ("words without any stop marker keep going for quite a while "
         "so the fallback cap matters here a lot")

FORMS = f"""## 1. Forms section

**0.2 GROUP TITLE IN BOLD** (trailing note text).

0.2a **Sub rule.** Separate rule text see 0.2.

0.2b **Another sub rule.** More separate text.

0.1 **AFTER NUMBER.** Body text here (D-200).

1.1 Plain rule with a first sentence. More words follow here.

1.2 {PLAIN}

**1.3** Title of three. Body text.

1.4** Title of four.** Body text.
"""


def run(source, tmp_path, *extra):
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "rules.md"
    src.write_text(source, encoding="utf-8")
    out_file = tmp_path / "batch.yaml"
    out, err = io.StringIO(), io.StringIO()
    rc = decisions_import.main(
        ["--source", str(src), "--out", str(out_file), *extra], out=out, err=err)
    return rc, out.getvalue(), err.getvalue(), out_file


def load(out_file):
    return yaml.safe_load(out_file.read_text(encoding="utf-8"))


def items_by_id(data):
    return {op["item"][0]["id"]: op["item"][0] for op in data["ops"]}


def test_parsing_counts(tmp_path):
    rc, out, _err, path = run(FIXTURE, tmp_path, "--report")
    assert rc == 0
    for line in ("rules 7", "items 7", "d-ids 6", "edges 9",
                 "dropped cycle edges: none", "unknown refs: none", "dropped lines: 0"):
        assert line in out
    assert len(load(path)["ops"]) == 7


def test_report_section_rule_counts(tmp_path):
    rc, out, _err, _path = run(FIXTURE, tmp_path, "--report")
    assert rc == 0
    assert "section 1. Alpha section: 4 rules" in out
    assert "section 2. Beta section: 3 rules" in out


def test_title_tag_did_extraction(tmp_path):
    rc, _out, _err, path = run(FIXTURE, tmp_path)
    assert rc == 0
    items = items_by_id(load(path))
    assert items["RULE-1-2"]["title"] == "RULE 1.2: Second rule."
    desc = items["RULE-1-2"]["description"]
    assert "Tags: gate: path/verb;" in desc
    assert "Decisions: D-101, D-102;" in desc
    assert "Decisions: D-100, D-105;" in items["RULE-2-3"]["description"]
    assert items["RULE-1-3"]["title"] == "RULE 1.3: Third rule."


def test_id_naming(tmp_path):
    rc, _out, _err, path = run(FIXTURE, tmp_path)
    assert rc == 0
    items = items_by_id(load(path))
    assert {"RULE-1-1", "RULE-0-2A", "RULE-2-3"} <= set(items)
    for ident in items:
        assert re.fullmatch(r"RULE-\d+-\d+[A-Z]?", ident), ident


def test_edge_from_see(tmp_path):
    rc, _out, _err, path = run(FIXTURE, tmp_path)
    assert rc == 0
    items = items_by_id(load(path))
    assert items["RULE-2-1"]["depends_on"] == ["plangraph:RULE-1-1"]
    assert items["RULE-0-2A"]["depends_on"] == ["plangraph:RULE-1-1"]


def test_edges_from_rules_slash_list(tmp_path):
    rc, _out, _err, path = run(FIXTURE, tmp_path)
    assert rc == 0
    items = items_by_id(load(path))
    assert items["RULE-1-3"]["depends_on"] == ["plangraph:RULE-1-1", "plangraph:RULE-1-2"]
    assert "plangraph:RULE-0-2A" in items["RULE-2-2"]["depends_on"]
    assert "plangraph:RULE-2-1" in items["RULE-2-2"]["depends_on"]


def test_self_reference_ignored(tmp_path):
    rc, _out, _err, path = run(FIXTURE, tmp_path)
    assert rc == 0
    item = items_by_id(load(path))["RULE-1-3"]
    assert "plangraph:RULE-1-3" not in item["depends_on"]
    tail = item["description"].rsplit("Depends on: ", 1)[1]
    assert "RULE-1-3" not in tail
    assert "RULE-1-1" in tail


def test_unknown_reference_reported(tmp_path):
    rc, out, _err, path = run(UNKNOWN, tmp_path, "--report")
    assert rc == 0
    assert "unknown refs: 1.1->9.9" in out
    items = items_by_id(load(path))
    assert items["RULE-1-1"]["depends_on"] == ["plangraph:RULE-1-2"]
    assert "9.9" not in items["RULE-1-1"]["description"].rsplit(
        "Depends on: ", 1)[1]


def test_cycle_drops_exactly_one_edge(tmp_path):
    rc, out, err, path = run(CYCLE, tmp_path, "--report")
    assert rc == 0
    assert "dropped cycle edges: 1.1->1.2" in out
    assert "dropped cycle edges: 1.1->1.2" in err
    assert out.count("->") == 1
    items = items_by_id(load(path))
    assert items["RULE-1-1"]["depends_on"] == []
    assert items["RULE-1-2"]["depends_on"] == ["plangraph:RULE-1-1"]


def test_continuation_lines(tmp_path):
    rc, _out, _err, path = run(FIXTURE, tmp_path)
    assert rc == 0
    desc = items_by_id(load(path))["RULE-1-2"]["description"]
    assert desc.count("\n") == 1
    assert desc.split("\n")[0] == (
        "**Second rule.** Refers to rule 1.1 and rule 0.2a (D-101, D-102) "
        "and continues here with extra words. `[gate: path/verb]`")
    assert desc.split("\n")[1] == (
        "Tags: gate: path/verb; Decisions: D-101, D-102; "
        "Depends on: RULE-0-2A RULE-1-1")


def test_priority_mapping(tmp_path):
    rc, _out, _err, path = run(FIXTURE, tmp_path)
    assert rc == 0
    items = items_by_id(load(path))
    assert items["RULE-2-1"]["priority"] == 7      # keep
    assert items["RULE-1-2"]["priority"] == 5      # gate
    assert items["RULE-2-2"]["priority"] == 9      # temporary
    assert items["RULE-1-3"]["priority"] == 5      # gate-todo wins over keep


def test_duplicate_id_exit_1(tmp_path):
    rc, _out, err, path = run(DUP, tmp_path)
    assert rc == 1
    assert "duplicate id RULE-1-1" in err
    assert not path.exists()


def test_rule_without_title_not_an_error(tmp_path):
    rc, _out, err, path = run(NOTITLE, tmp_path)
    assert rc == 0
    assert "error:" not in err and "dropped lines: 0" in err
    items = items_by_id(load(path))
    assert items["RULE-1-2"]["title"] == "RULE 1.2: Missing bold title here."


def test_bold_before_and_after_number_forms(tmp_path):
    rc, _out, err, path = run(FORMS, tmp_path)
    assert rc == 0
    assert "error:" not in err and "group headings dropped: 0.2" in err
    items = items_by_id(load(path))
    # bare group heading 0.2 (no tag, no D-id) is dropped, its sub-rules stay
    assert {"RULE-0-2A", "RULE-0-2B", "RULE-0-1",
            "RULE-1-1", "RULE-1-2", "RULE-1-3", "RULE-1-4"} == set(items)
    assert items["RULE-0-2A"]["title"] == "RULE 0.2a: Sub rule."
    assert items["RULE-0-2B"]["title"] == "RULE 0.2b: Another sub rule."
    assert items["RULE-0-1"]["title"] == "RULE 0.1: AFTER NUMBER."
    assert items["RULE-1-1"]["title"] == "RULE 1.1: Plain rule with a first sentence."
    assert items["RULE-1-2"]["title"] == "RULE 1.2: " + PLAIN[:60]
    assert items["RULE-1-3"]["title"] == "RULE 1.3: Title of three."
    assert items["RULE-1-4"]["title"] == "RULE 1.4: Title of four."


def test_title_fallback_caps():
    text = f"## 1. S\n\n1.1 {LONGSENT}\n\n1.2 {NOSEMI}\n"
    rules = decisions_import.parse_rules(text)
    titles = {rule["number"]: rule["title"] for rule in rules}
    assert titles["1.1"] == LONGSENT[:120]   # first sentence, max 120
    assert titles["1.2"] == NOSEMI[:60]      # no title at all: first 60


def test_output_yaml_shape_and_dids(tmp_path):
    rc, _out, _err, path = run(FIXTURE, tmp_path)
    assert rc == 0
    data = load(path)
    assert set(data) == {"batch", "principal", "source", "source_sha256", "ops"}
    assert data["principal"] == "router"
    assert data["source"] == "rules.md"
    src = tmp_path / "rules.md"
    assert data["source_sha256"] == hashlib.sha256(src.read_bytes()).hexdigest()
    for op in data["ops"]:
        assert set(op) == {"op", "item"} and op["op"] == "create"
        assert isinstance(op["item"], list) and len(op["item"]) == 1
        item = op["item"][0]
        assert set(item) == {"project", "id", "kind", "owner", "track", "priority",
                             "review_tier", "evidence_kinds", "depends_on", "title", "description"}
        assert item["project"] == "plangraph" and item["kind"] == "decision"
        assert item["owner"] == "operator" and item["track"] == "ROUTING"
        assert item["review_tier"] == "none" and item["evidence_kinds"] == ["decision"]
        assert len(item["title"]) <= 120
    text = "".join(op["item"][0]["description"] for op in data["ops"])
    for did in sorted(set(re.findall(r"D-\d+", FIXTURE))):
        assert did in text


def test_source_path_relative_to_out_dir(tmp_path):
    rc, _out, _err, path = run(FIXTURE, tmp_path)
    assert rc == 0
    out_file = tmp_path / "gen" / "batch.yaml"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    rc = decisions_import.main(
        ["--source", str(tmp_path / "rules.md"), "--out", str(out_file)])
    assert rc == 0
    data = load(out_file)
    assert data["source"] == os.path.join("..", "rules.md")
    assert apply_batch.resolve_source(str(out_file), data["source"]) == os.path.realpath(tmp_path / "rules.md")


def test_apply_batch_loads_and_validates(tmp_path):
    rc, _out, _err, path = run(FIXTURE, tmp_path)
    assert rc == 0
    data = apply_batch.load_batch(str(path))
    assert set(data) == {"batch", "principal", "source", "source_sha256", "ops"}
    assert data["source"] == "rules.md"
    assert data["source_sha256"] == hashlib.sha256((tmp_path / "rules.md").read_bytes()).hexdigest()
    for op in data["ops"]:
        assert set(op) == {"op", "item"}
        apply_batch.validate_op(op)


def test_determinism_two_runs(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / "rules.md"
    src.write_text(FIXTURE, encoding="utf-8")
    blobs = []
    for name in ("one.yaml", "two.yaml"):
        out_file = tmp_path / name
        rc = decisions_import.main(["--source", str(src), "--out", str(out_file)])
        assert rc == 0
        blobs.append(out_file.read_bytes())
    assert blobs[0] == blobs[1]


def test_verify_text_dag_mismatch():
    rule = {"number": "1.1", "description": "T\nDepends on: RULE-1-2",
            "depends_on": ["plangraph:RULE-1-3"]}
    with pytest.raises(ValueError, match="Depends on line"):
        decisions_import.verify_text_dag([rule])


def test_continuation_paragraph_after_blank_line(tmp_path):
    text = ("## 1. S\n\n1.1 **First rule.** Paragraph 1.\n\n"
            "Paragraph 2 see 1.2 (D-101) `[gate: test]`\n\n1.2 **Second.** Text.\n")
    rc, _out, _err, path = run(text, tmp_path)
    assert rc == 0
    item = items_by_id(load(path))["RULE-1-1"]
    assert "Paragraph 2 see 1.2" in item["description"]
    assert "D-101" in item["description"]
    assert "Tags: gate: test;" in item["description"]
    assert item["depends_on"] == ["plangraph:RULE-1-2"]
    assert item["priority"] == 5


def test_dropped_lines_counted_and_reported(tmp_path):
    text = "Drop 1\nDrop 2\n\n## 1. S\n\nDrop 6\nDrop 7\nDrop 8\nDrop 9\n\n1.1 **R.** T\n"
    rc, out, _err, _path = run(text, tmp_path, "--report")
    assert rc == 0
    assert "dropped lines: 6 (lines 1, 2, 6, 7, 8)" in out


def test_bold_opens_on_rule_line_closes_on_continuation(tmp_path):
    text = "## 1. S\n\n**1.1 TITLE\nCLOSES HERE.** Body text. `[keep]`\n"
    rc, _out, _err, path = run(text, tmp_path)
    assert rc == 0
    item = items_by_id(load(path))["RULE-1-1"]
    assert item["title"] == "RULE 1.1: TITLE CLOSES HERE."
    assert "**" not in item["title"]
    assert item["description"].startswith("**TITLE CLOSES HERE.** Body text.")


def test_group_heading_is_reported_not_silent(tmp_path):
    rc, out, _err, _path = run(FORMS, tmp_path, "--report")
    assert rc == 0
    assert "group headings dropped: 0.2" in out


def test_group_heading_with_sentence_kept(tmp_path):
    text = "## 1. S\n\n**0.2 TITLE** (note). Sentence.\n\n0.2a **Sub.** Text.\n"
    rc, _out, err, path = run(text, tmp_path)
    assert rc == 0
    items = items_by_id(load(path))
    assert "RULE-0-2" in items
    assert "Sentence." in items["RULE-0-2"]["description"]
    assert "group headings dropped: none" in err


def test_heading_only_bold_title_dropped(tmp_path):
    text = "## 1. S\n\n**0.2 ONLY TITLE**\n\n0.2a **Sub.** Text.\n"
    rc, _out, err, path = run(text, tmp_path)
    assert rc == 0
    assert "RULE-0-2" not in items_by_id(load(path))
    assert "group headings dropped: 0.2" in err


def test_summary_always_printed_to_stderr_without_report(tmp_path):
    rc, out, err, _path = run(FIXTURE, tmp_path)
    assert rc == 0 and out == ""
    assert "dropped cycle edges: none\nunknown refs: none\ndropped lines: 0\ngroup headings dropped: none\n" == err


def test_value_error_from_build_catches_and_exits_1(tmp_path, monkeypatch):
    monkeypatch.setattr(decisions_import, "verify_text_dag",
                        lambda rules, prefix=None: (_ for _ in ()).throw(ValueError("bad dag edge")))
    rc, _out, err, _path = run(FIXTURE, tmp_path)
    assert rc == 1
    assert "error: bad dag edge\n" in err
