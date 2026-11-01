"""tools/decisions_render.py: decision items rendered back into rules markdown.

Every string here is synthetic; no real text, names or paths appear.
"""
import io
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import decisions_import  # noqa: E402
import decisions_render  # noqa: E402

SOURCE = """# Synthetic rules

## 1. Alpha section

**0.2 GROUP TITLE** (note).

0.2a **Sub one.** Text one here.

0.2b **Sub two.** Text two here.

1.1 **Multi rule.** First line here
continues on the second line
and the third (D-300) `[keep]`

**1.2** Bold before number. Body text (D-301) `[gate: x/y]`

## 2. Beta section

2.1 **Plain rule.** Plain text `[temporary]`
"""

NORMALIZED = """# Synthetic rules

## 1. Alpha section

**0.2 GROUP TITLE** (note).

0.2a **Sub one.** Text one here.

0.2b **Sub two.** Text two here.

1.1 **Multi rule.** First line here continues on the second line and the third (D-300) `[keep]`

1.2 Bold before number. Body text (D-301) `[gate: x/y]`

## 2. Beta section

2.1 **Plain rule.** Plain text `[temporary]`
"""


def run(args):
    out, err = io.StringIO(), io.StringIO()
    rc = decisions_render.main(args, out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def pipeline(tmp_path, source_text, tag):
    """skeleton(source) -> importer items(source) -> render -> out file."""
    src = tmp_path / ("rules-%s.md" % tag)
    src.write_text(source_text, encoding="utf-8")
    skel = tmp_path / ("skel-%s.md" % tag)
    batch = tmp_path / ("batch-%s.yaml" % tag)
    out = tmp_path / ("out-%s.md" % tag)
    assert run(["skeleton", "--source", str(src), "--out", str(skel)])[0] == 0
    imp_out, imp_err = io.StringIO(), io.StringIO()
    assert decisions_import.main(
        ["--source", str(src), "--out", str(batch)],
        out=imp_out, err=imp_err) == 0
    rc, _o, err = run(["render", "--skeleton", str(skel), "--items", str(batch),
                       "--out", str(out)])
    assert rc == 0, err
    return skel, batch, out


def test_round_trip_renders_normalized_source(tmp_path):
    skel, _batch, out = pipeline(tmp_path, SOURCE, "src")
    assert out.read_text(encoding="utf-8") == NORMALIZED
    text = skel.read_text(encoding="utf-8")
    assert "**0.2 GROUP TITLE** (note)." in text   # group heading literal
    assert "continues on the second line" not in text
    assert text.count("{{RULE-") == 5


def test_render_of_normalized_file_is_fixed_point(tmp_path):
    _s, _b, first = pipeline(tmp_path, SOURCE, "src")
    _s2, _b2, second = pipeline(tmp_path, NORMALIZED, "norm")
    assert second.read_text(encoding="utf-8") == NORMALIZED
    assert second.read_bytes() == first.read_bytes()


def test_check_passes_then_hand_edit_fails(tmp_path):
    skel, batch, out = pipeline(tmp_path, SOURCE, "src")
    args = ["check", "--skeleton", str(skel), "--items", str(batch)]
    rc, _o, err = run(args + ["--target", str(out)])
    assert rc == 0 and err == ""
    out.write_text(NORMALIZED.replace("Plain text", "Hand edit"),
                   encoding="utf-8")
    rc, _o, err = run(args + ["--target", str(out)])
    assert rc == 1
    assert len(err.splitlines()) <= 40
    assert "Hand edit" in err


def test_missing_item_error_names_id(tmp_path):
    skel, batch, out = pipeline(tmp_path, SOURCE, "src")
    items = [op["item"][0] for op in yaml.safe_load(batch.read_text())["ops"]]
    items = [it for it in items if it["id"] != "RULE-0-2B"]
    path = tmp_path / "items.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    rc, _o, err = run(["render", "--skeleton", str(skel), "--items", str(path),
                       "--out", str(out)])
    assert rc == 1 and "RULE-0-2B" in err


def test_extra_item_error_names_id(tmp_path):
    skel, batch, _out = pipeline(tmp_path, SOURCE, "src")
    items = [op["item"][0] for op in yaml.safe_load(batch.read_text())["ops"]]
    items.append({"id": "RULE-9-9", "description": "9.9 Orphan text."})
    path = tmp_path / "items.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    out2 = tmp_path / "out2.md"
    rc, _o, err = run(["render", "--skeleton", str(skel), "--items", str(path),
                       "--out", str(out2)])
    assert rc == 1 and "RULE-9-9" in err
    assert not out2.exists()


def test_json_and_batch_yaml_items_agree(tmp_path):
    skel, batch, out = pipeline(tmp_path, SOURCE, "src")
    items = [op["item"][0] for op in yaml.safe_load(batch.read_text())["ops"]]
    path = tmp_path / "items.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    out2 = tmp_path / "out2.md"
    rc, _o, err = run(["render", "--skeleton", str(skel), "--items", str(path),
                       "--out", str(out2)])
    assert rc == 0, err
    assert out2.read_bytes() == out.read_bytes()


def test_trailing_newline_state_preserved(tmp_path):
    src = tmp_path / "rules.md"
    src.write_text("## 1. S\n\n1.1 **One.** Text.", encoding="utf-8")
    skel = tmp_path / "skel.md"
    assert run(["skeleton", "--source", str(src), "--out", str(skel)])[0] == 0
    assert skel.read_text(encoding="utf-8") == "## 1. S\n\n{{RULE-1-1}}"
    path = tmp_path / "items.json"
    path.write_text(json.dumps([{"id": "RULE-1-1",
                                 "description": "**One.** Text.\nTags: keep"}]),
                    encoding="utf-8")
    out = tmp_path / "out.md"
    rc, _o, err = run(["render", "--skeleton", str(skel), "--items", str(path),
                       "--out", str(out)])
    assert rc == 0, err
    assert out.read_text(encoding="utf-8") == "## 1. S\n\n1.1 **One.** Text."
