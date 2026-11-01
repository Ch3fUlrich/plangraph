"""Hermetic checks for docs/skills/rule-disposition.md."""

import ast
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "skills" / "rule-disposition.md"
CARD_RE = re.compile(r"role card: (planner|orchestrator|worker)")
REF_RE = re.compile(r"^(\S+\.py)::(test_\w+)$")


def _rows():
    out = []
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| R-"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        assert len(cells) == 4, line
        out.append(cells)
    return out


def _gaps():
    assert "## Gaps" in DOC.read_text(encoding="utf-8")
    section = DOC.read_text(encoding="utf-8").split("## Gaps", 1)[1]
    return set(re.findall(r"\bR-[a-z]+-\d+\b", section))


def test_table_has_67_unique_ids():
    ids = [row[0] for row in _rows()]
    assert len(ids) == 67
    assert len(set(ids)) == 67


def test_disposition_counts():
    counts = Counter(row[1] for row in _rows())
    assert counts == {"delete": 28, "code": 22, "card": 17}


def test_every_row_has_a_why():
    for row in _rows():
        assert row[3], row[0]


def test_delete_rows_have_no_enforcement():
    for rid, disp, enf, _ in _rows():
        if disp == "delete":
            assert enf == "-", rid


def test_card_rows_name_a_role():
    for rid, disp, enf, _ in _rows():
        if disp == "card":
            assert CARD_RE.fullmatch(enf), (rid, enf)


def test_code_rows_are_gaps_or_named_existing_tests():
    gaps = _gaps()
    for rid, disp, enf, _ in _rows():
        if disp != "code":
            continue
        if "none" in enf:
            assert rid in gaps, rid
            continue
        match = REF_RE.match(enf)
        assert match, (rid, enf)
        path, name = match.groups()
        target = ROOT / path
        assert target.is_file(), (rid, path)
        tree = ast.parse(target.read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert name in defined, (rid, name)


def test_gaps_are_none_code_rows():
    code = {row[0]: row for row in _rows() if row[1] == "code"}
    for rid in _gaps():
        assert rid in code, rid
        assert "none" in code[rid][2], rid
