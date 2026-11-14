"""Synthetic PLAN-LINT contracts against the real Rust report binary."""
import copy
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def clean_env():
    return {k: v for k, v in os.environ.items()
            if k not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
            and not k.startswith("GIT_CONFIG")}


@pytest.fixture(scope="session")
def binary():
    subprocess.run(["cargo", "build", "-q", "-p", "pg-worker-core", "--bin", "pg-plan-lint"],
                   cwd=ROOT / "rust", env=clean_env(), check=True)
    return ROOT / "rust/target/debug/pg-plan-lint"


def card(key="TASK-A"):
    return {"id": key, "title": "Serialize a sample response", "kind": "lane",
            "owner": "build-L1", "track": "SAMPLE", "est_h": 2,
            "description": "Why: D-100 defines the format. How: src/sample.rs",
            "card": {"write_set": ["src/sample.rs"], "read_set": [],
                     "check_cmd": ["cargo test sample"]},
            "criteria": [
                {"id": "A", "text": "Returns status 200 for sample input", "check_ref": "check[0]::ok", "class": "code"},
                {"id": "B", "text": "Rejects missing input with error empty", "check_ref": "check[0]::empty", "class": "code"},
                {"id": "C", "text": "Writes result bytes in ascending order", "check_ref": "check[0]::order", "class": "code"}]}


def report(binary, tmp_path, items, running=()):
    source = tmp_path / "snapshot.json"
    source.write_text(json.dumps({"items": items, "running": running}))
    result = subprocess.run([str(binary), "--snapshot", str(source), "--json"],
                            text=True, capture_output=True, env=clean_env())
    assert result.returncode in (0, 1), result.stderr
    return json.loads(result.stdout)


# Twenty explicitly hand-scored generic fixtures: one point per distinct finding,
# bounded at zero. Missing commands also invalidate their criteria references.
CASES = [
    ("clean", {}, None, 10),
    ("owner", {"owner": "unknown"}, "owner_unknown", 9),
    ("track", {"track": ""}, "track_missing", 9),
    ("title", {"title": "x" * 121}, "title_long", 9),
    ("why", {"description": "How: src/sample.rs"}, "why_missing", 9),
    ("how", {"description": "Why: D-100"}, "how_missing", 9),
    ("estimate", {"est_h": 5}, "too_big", 9),
    ("followup", {"id": "TASK-FIX"}, "followup_key", 9),
    ("vague", {"row": "Ensure robust output"}, "vague_word", 9),
    ("process", {"row": "Merged, CI green, seat ACCEPT"}, "process_only", 9),
    ("count", {"criteria_count": 2}, "count_range", 9),
    ("class", {"classes": "judged"}, "no_code_row", 9),
    ("ref", {"ref": ""}, "clause_no_check", 9),
    ("command", {"command": []}, ["clause_no_check", "no_check"], 8),
    ("syntax", {"command": ["cargo test '"]}, "check_not_runnable", 9),
    ("path", {"paths": ["../outside.rs"]}, "bad_path", 9),
    ("paths", {"paths": [f"src/{i}.rs" for i in range(7)]}, "too_many_write_paths", 9),
    ("universal", {"row": "Every response has status 200"}, "unenumerated_universal", 9),
    ("stated", {"row": "Returns the 5 fields listed in docs/schema.md"}, "stated_count", 9),
    ("edge", {"depends_on": ["MISSING"]}, "edge_no_reason", 9),
]


def mutate(changes):
    item = card()
    for key, value in changes.items():
        if key == "row": item["criteria"][0]["text"] = value
        elif key == "ref": item["criteria"][0]["check_ref"] = value
        elif key == "classes":
            for row in item["criteria"]: row["class"] = value
        elif key == "criteria_count": item["criteria"] = item["criteria"][:value]
        elif key == "command": item["card"]["check_cmd"] = value
        elif key == "paths": item["card"]["write_set"] = value
        else: item[key] = value
    return item


@pytest.mark.parametrize("name,changes,code,score", CASES)
def test_hand_scores(binary, tmp_path, name, changes, code, score):
    result = report(binary, tmp_path, [mutate(changes)])[0]
    assert result["score"] == score, result
    assert result["codes"] == (code if isinstance(code, list) else [code] if code else []), result
    if code:
        assert "fix:" in result["message"].lower()


def test_combined_and_accepting_counterparts(binary, tmp_path):
    changes = {"owner": "unknown", "est_h": 9, "row": "Ensure output", "ref": ""}
    result = report(binary, tmp_path, [mutate(changes)])[0]
    assert set(result["codes"]) == {"owner_unknown", "too_big", "vague_word", "clause_no_check"}
    assert all(code in result["message"] for code in result["codes"])
    assert report(binary, tmp_path, [card()])[0]["codes"] == []


def test_context_edges_overlap_and_duplicates(binary, tmp_path):
    first, second = card(), card("TASK-B")
    second["title"] = "Parse a sample request"
    assert "duplicate_criteria" in report(binary, tmp_path, [first, second])[0]["codes"]
    second["track"] = "OTHER"
    assert report(binary, tmp_path, [first, second])[0]["codes"] == []
    second["title"] = first["title"]
    assert "title_dup" in report(binary, tmp_path, [first, second])[0]["codes"]
    second["title"] = "Parse a sample request"
    first["depends_on"] = [second["id"]]
    assert "edge_no_reason" in report(binary, tmp_path, [first, second])[0]["codes"]
    first["card"]["read_set"] = ["src/sample.rs"]
    assert "edge_no_reason" not in report(binary, tmp_path, [first, second])[0]["codes"]
    second["kind"] = "decision"
    first["card"]["read_set"] = []
    assert "edge_no_reason" not in report(binary, tmp_path, [first, second])[0]["codes"]
    assert "write_overlap" in report(binary, tmp_path, [first], [second])[0]["codes"]
    second["card"]["write_set"] = ["tests/other.rs"]
    assert "write_overlap" not in report(binary, tmp_path, [first], [second])[0]["codes"]


@pytest.mark.parametrize("suffix", ["LOWS", "FIX", "FU", "FOLLOWUP", "SQ12", "R2"])
def test_followup_suffixes(binary, tmp_path, suffix):
    assert "followup_key" in report(binary, tmp_path, [card("TASK-" + suffix)])[0]["codes"]
    assert "followup_key" not in report(binary, tmp_path, [card("TASK-" + suffix + "-CORE")])[0]["codes"]

@pytest.mark.parametrize("changes", [
    {"row": "Every response in [empty, valid, invalid] has a named status"},
    {"row": "Returns fields listed in docs/schema.md"},
    {"paths": ["src/*.rs", "tests/*.rs"]},
    {"est_h": 4},
    {"description": "Why: plan section 2. How: src/sample.rs"},
    {"command": ["cargo test 'sample input'"]},
])
def test_accepting_boundaries(binary, tmp_path, changes):
    assert report(binary, tmp_path, [mutate(changes)])[0]["codes"] == []


def test_similarity_matches_difflib(binary, tmp_path):
    from difflib import SequenceMatcher
    pairs = [("abc " * 80, "abc " * 79 + "xyz "),
             ("A" * 220, "B" + "A" * 220),
             ("status 200 " * 10, "status 201 " * 10),
             ("écho🙂value" * 8, "écho🙂values" * 8),
             ("abcdef012345", "fedcba543210")]
    for a, b in pairs:
        first, second = card(), card("TASK-B")
        second["title"] = "Parse a sample request"
        first["criteria"][0]["text"], second["criteria"][0]["text"] = a, b
        a = "\n".join(r["text"].lower() for r in first["criteria"])
        b = "\n".join(r["text"].lower() for r in second["criteria"])
        actual = report(binary, tmp_path, [first, second])
        assert ("duplicate_criteria" in actual[0]["codes"]) == (SequenceMatcher(None, a, b).ratio() >= .85)
        assert ("duplicate_criteria" in actual[1]["codes"]) == (SequenceMatcher(None, b, a).ratio() >= .85)


def test_never_executes_checks(binary, tmp_path):
    marker = tmp_path / "executed"
    item = card()
    item["card"]["check_cmd"] = [f"touch {marker}"]
    assert report(binary, tmp_path, [item])[0]["codes"] == []
    assert not marker.exists()


@pytest.mark.parametrize("text", ["Returns merged values in ascending order",
                                  "Returns 2 rows in ascending order"])
def test_concrete_states_are_not_process_or_external_counts(binary, tmp_path, text):
    assert report(binary, tmp_path, [mutate({"row": text})])[0]["codes"] == []

@pytest.mark.parametrize("reference", ["check[99]", "check[0", "check[0]garbage",
                                       "heldout_check", "check[0],"])
def test_invalid_check_references(binary, tmp_path, reference):
    assert "clause_no_check" in report(binary, tmp_path, [mutate({"ref": reference})])[0]["codes"]


def test_clause_checks_are_per_clause(binary, tmp_path):
    item = mutate({"row": "Returns status 200; Rejects empty input", "ref": "check[0]::ok"})
    assert "clause_no_check" in report(binary, tmp_path, [item])[0]["codes"]
    item["criteria"][0]["check_ref"] = "check[0]::ok,check[0]::empty"
    assert report(binary, tmp_path, [item])[0]["codes"] == []


def test_rust_implementation_total_size():
    files = [ROOT / "rust/worker-core/src/plan_lint.rs",
             ROOT / "rust/worker-core/src/plan_lint/similarity.rs",
             ROOT / "rust/worker-core/src/bin/pg-plan-lint.rs"]
    assert sum(len(path.read_text().splitlines()) for path in files) <= 400
