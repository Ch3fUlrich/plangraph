"""MODEL-NAMES: tools/names_backfill_dryrun.py lists candidates, never writes."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "names_backfill_dryrun.py"

FOLDED = ("The folded epsilon description runs well past eighty characters, so the "
          "listing has to cut the note head right there.")
NOTE = "description: " + FOLDED


def snapshot(tmp_path, items, name="plan.snapshot.json"):
    """A plan snapshot exactly as keeper writes it (mapping with an items list)."""
    path = tmp_path / name
    path.write_text(json.dumps({"generated": "2026-10-17T00:00:00Z", "head": "abc1234",
                                "items": items}, indent=1), encoding="utf-8")
    return path


def items():
    return [
        # id-like title, description present -> title_is_id only
        {"key": "demo:alpha_one-two", "title": "ALPHA-1", "state": "open",
         "description": "Alpha is described", "note": None},
        # named title, no description, folded description in the note
        {"key": "demo:beta-three", "title": "Write the beta docs", "state": "open",
         "description": None, "note": "description: Beta description from the folded note"},
        # clean item: never listed
        {"key": "demo:gamma", "title": "Gamma work", "state": "open",
         "description": "Gamma described", "note": None},
        # done item: locked, never listed
        {"key": "demo:delta-nine", "title": "DELTA-9", "state": "done",
         "description": None, "note": None},
        # both findings: one line per code
        {"key": "demo:epsilon", "title": "EPSILON-2", "state": "open",
         "description": None, "note": NOTE},
    ]


EXPECTED = [
    "demo:beta-three | no_description | Write the beta docs | Demo:beta Three | "
    "description: Beta description from the folded note",
    "demo:epsilon | no_description | EPSILON-2 | Demo:epsilon | description: "
    + FOLDED[:80],
    "demo:alpha_one-two | title_is_id | ALPHA-1 | Demo:alpha One Two | "
    "description: MISSING",
    "demo:epsilon | title_is_id | EPSILON-2 | Demo:epsilon | description: "
    + FOLDED[:80],
    "count: 4",
]


def run(path, *extra):
    return subprocess.run([sys.executable, str(TOOL), str(path), *extra],
                          capture_output=True, text=True)


def test_output_lines_and_trailing_count(tmp_path):
    assert len(FOLDED) > 80                      # the head really is truncated
    r = run(snapshot(tmp_path, items()))
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == EXPECTED
    # only the note head is ever printed, never the whole note
    assert FOLDED not in r.stdout and FOLDED[80:] not in r.stdout
    # the done item and the clean item never appear
    assert "demo:delta-nine" not in r.stdout and "demo:gamma" not in r.stdout


def test_output_is_deterministic(tmp_path):
    path = snapshot(tmp_path, items())
    first, second = run(path), run(path)
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout


def test_json_is_the_same_list(tmp_path):
    r = run(snapshot(tmp_path, items()), "--json")
    assert r.returncode == 0, r.stderr
    rows = json.loads(r.stdout)
    assert len(rows) == 4
    assert [(row["code"], row["key"]) for row in rows] == [
        ("no_description", "demo:beta-three"),
        ("no_description", "demo:epsilon"),
        ("title_is_id", "demo:alpha_one-two"),
        ("title_is_id", "demo:epsilon"),
    ]
    lines = run(snapshot(tmp_path, items())).stdout.splitlines()
    for row, line in zip(rows, lines[:-1]):
        assert line == (f"{row['key']} | {row['code']} | {row['title']} | "
                        f"{row['proposed']} | description: {row['description']}")
    assert lines[-1] == f"count: {len(rows)}"


def test_missing_input_exits_2(tmp_path):
    assert run(tmp_path / "nope.json").returncode == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert run(bad).returncode == 2


def test_source_has_no_write_or_network_verb():
    src = TOOL.read_text(encoding="utf-8")
    for banned in ("apply_ops", "urlopen", "requests", "subprocess"):
        assert banned not in src
    assert "LIVE backfill is a live step: this script only lists." in src
