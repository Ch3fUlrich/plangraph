"""tools/decisions_render.py: decision items rendered back into rules markdown.

Every string here is synthetic; no real text, names or paths appear.
"""
import io
import json
import sys

import pytest
import yaml

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


@pytest.fixture
def mods(tools_importable):
    """(decisions_render, decisions_import) while tools/ is importable."""
    import decisions_import
    import decisions_render
    return decisions_render, decisions_import


@pytest.fixture
def sys_path_restored():
    """Torn down last: fail the test if sys.path was left modified behind."""
    before = list(sys.path)
    yield
    assert sys.path == before


def run(render, args):
    out, err = io.StringIO(), io.StringIO()
    rc = render.main(args, out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def pipeline(mods, tmp_path, source_text, tag):
    """skeleton(source) -> importer items(source) -> render -> out file."""
    render, imp = mods
    src = tmp_path / ("rules-%s.md" % tag)
    src.write_bytes(source_text.encode("utf-8"))
    skel = tmp_path / ("skel-%s.md" % tag)
    batch = tmp_path / ("batch-%s.yaml" % tag)
    out = tmp_path / ("out-%s.md" % tag)
    assert run(render, ["skeleton", "--source", str(src), "--out", str(skel)])[0] == 0
    imp_out, imp_err = io.StringIO(), io.StringIO()
    assert imp.main(
        ["--source", str(src), "--out", str(batch)],
        out=imp_out, err=imp_err) == 0
    rc, _o, err = run(render, ["render", "--skeleton", str(skel), "--items", str(batch),
                               "--out", str(out)])
    assert rc == 0, err
    return skel, batch, out


def test_round_trip_renders_normalized_source(mods, tmp_path):
    render, _imp = mods
    skel, _batch, out = pipeline(mods, tmp_path, SOURCE, "src")
    assert out.read_bytes().decode("utf-8") == NORMALIZED
    text = skel.read_bytes().decode("utf-8")
    assert "**0.2 GROUP TITLE** (note)." in text   # group heading literal
    assert "continues on the second line" not in text
    assert text.count("{{RULE-") == 5


def test_render_of_normalized_file_is_fixed_point(mods, tmp_path):
    _s, _b, first = pipeline(mods, tmp_path, SOURCE, "src")
    _s2, _b2, second = pipeline(mods, tmp_path, NORMALIZED, "norm")
    assert second.read_bytes().decode("utf-8") == NORMALIZED
    assert second.read_bytes() == first.read_bytes()


def test_check_passes_then_hand_edit_fails(mods, tmp_path):
    render, _imp = mods
    skel, batch, out = pipeline(mods, tmp_path, SOURCE, "src")
    args = ["check", "--skeleton", str(skel), "--items", str(batch)]
    rc, _o, err = run(render, args + ["--target", str(out)])
    assert rc == 0 and err == ""
    out.write_bytes(NORMALIZED.replace("Plain text", "Hand edit")
                    .encode("utf-8"))
    rc, _o, err = run(render, args + ["--target", str(out)])
    assert rc == 1
    assert len(err.splitlines()) <= 40
    assert "Hand edit" in err


def test_missing_item_error_names_id(mods, tmp_path):
    render, _imp = mods
    skel, batch, out = pipeline(mods, tmp_path, SOURCE, "src")
    items = [op["item"][0] for op in yaml.safe_load(batch.read_text())["ops"]]
    items = [it for it in items if it["id"] != "RULE-0-2B"]
    path = tmp_path / "items.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    rc, _o, err = run(render, ["render", "--skeleton", str(skel), "--items", str(path),
                               "--out", str(out)])
    assert rc == 1 and "RULE-0-2B" in err


def test_extra_item_error_names_id(mods, tmp_path):
    render, _imp = mods
    skel, batch, _out = pipeline(mods, tmp_path, SOURCE, "src")
    items = [op["item"][0] for op in yaml.safe_load(batch.read_text())["ops"]]
    items.append({"id": "RULE-9-9", "description": "9.9 Orphan text."})
    path = tmp_path / "items.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    out2 = tmp_path / "out2.md"
    rc, _o, err = run(render, ["render", "--skeleton", str(skel), "--items", str(path),
                               "--out", str(out2)])
    assert rc == 1 and "RULE-9-9" in err
    assert not out2.exists()


def test_json_and_batch_yaml_items_agree(mods, tmp_path):
    render, _imp = mods
    skel, batch, out = pipeline(mods, tmp_path, SOURCE, "src")
    items = [op["item"][0] for op in yaml.safe_load(batch.read_text())["ops"]]
    path = tmp_path / "items.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    out2 = tmp_path / "out2.md"
    rc, _o, err = run(render, ["render", "--skeleton", str(skel), "--items", str(path),
                               "--out", str(out2)])
    assert rc == 0, err
    assert out2.read_bytes() == out.read_bytes()


def test_trailing_newline_state_preserved(mods, tmp_path):
    render, _imp = mods
    src = tmp_path / "rules.md"
    src.write_bytes("## 1. S\n\n1.1 **One.** Text.".encode("utf-8"))
    skel = tmp_path / "skel.md"
    assert run(render, ["skeleton", "--source", str(src), "--out", str(skel)])[0] == 0
    assert skel.read_bytes().decode("utf-8") == "## 1. S\n\n{{RULE-1-1}}"
    path = tmp_path / "items.json"
    path.write_text(json.dumps([{"id": "RULE-1-1",
                                 "description": "**One.** Text.\nTags: keep"}]),
                    encoding="utf-8")
    out = tmp_path / "out.md"
    rc, _o, err = run(render, ["render", "--skeleton", str(skel), "--items", str(path),
                               "--out", str(out)])
    assert rc == 0, err
    assert out.read_bytes().decode("utf-8") == "## 1. S\n\n1.1 **One.** Text."


def test_crlf_source_round_trips_byte_exact(mods, tmp_path):
    """A CRLF source renders byte-equal to its CRLF target (seat: EOL fix)."""
    render, _imp = mods
    crlf = SOURCE.replace("\n", "\r\n")
    skel, batch, out = pipeline(mods, tmp_path, crlf, "crlf")
    want = NORMALIZED.replace("\n", "\r\n").encode("utf-8")
    assert out.read_bytes() == want
    assert b"{{RULE-1-1}}\r\n" in skel.read_bytes()  # placeholder keeps the EOL
    rc, _o, err = run(render, ["check", "--skeleton", str(skel), "--items",
                               str(batch), "--target", str(out)])
    assert rc == 0 and err == ""
    _s2, _b2, again = pipeline(mods, tmp_path, want.decode("utf-8"), "crlf2")
    assert again.read_bytes() == want   # fixed point, byte for byte


def test_duplicate_item_ids_rejected(mods, tmp_path):
    """Two items with one id error out; no silent last-wins (seat MED)."""
    render, _imp = mods
    skel, batch, _out = pipeline(mods, tmp_path, SOURCE, "dup")
    items = [op["item"][0] for op in yaml.safe_load(batch.read_text())["ops"]]
    items.append(dict(items[0]))                    # the same id twice
    path = tmp_path / "items.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    out2 = tmp_path / "out2.md"
    rc, _o, err = run(render, ["render", "--skeleton", str(skel), "--items",
                               str(path), "--out", str(out2)])
    assert rc == 1 and "duplicate" in err
    assert items[0]["id"] in err
    assert not out2.exists()


def test_duplicate_placeholder_rejected(mods, tmp_path):
    """The same placeholder twice errors out; no silent collapse (seat MED)."""
    render, _imp = mods
    skel = tmp_path / "skel.md"
    skel.write_bytes(b"## 1. S\n\n{{RULE-1-1}}\n\n{{RULE-1-1}}\n")
    items = tmp_path / "items.json"
    items.write_text(json.dumps([{"id": "RULE-1-1",
                                  "description": "**One.** Text."}]),
                     encoding="utf-8")
    out = tmp_path / "out.md"
    rc, _o, err = run(render, ["render", "--skeleton", str(skel), "--items",
                               str(items), "--out", str(out)])
    assert rc == 1 and "twice" in err
    assert "RULE-1-1" in err
    assert not out.exists()


def test_io_errors_name_the_path(mods, tmp_path):
    """RenderError messages carry the path, not just the exception class."""
    render, _imp = mods
    skel = tmp_path / "skel.md"
    skel.write_bytes(b"## 1. S\n\n{{RULE-1-1}}\n")
    out = tmp_path / "out.md"
    missing = tmp_path / "no-such-file.json"
    rc, _o, err = run(render, ["render", "--skeleton", str(skel), "--items",
                               str(missing), "--out", str(out)])
    assert rc == 1 and "cannot read items" in err and str(missing) in err
    broken = tmp_path / "broken.json"               # str(exc) carries no path
    broken.write_bytes(b"\xff\xfe not utf-8")
    rc, _o, err = run(render, ["render", "--skeleton", str(skel), "--items",
                               str(broken), "--out", str(out)])
    assert rc == 1 and str(broken) in err
    unparsable = tmp_path / "bad.yaml"
    unparsable.write_bytes(b"[unclosed")
    rc, _o, err = run(render, ["render", "--skeleton", str(skel), "--items",
                               str(unparsable), "--out", str(out)])
    assert rc == 1 and "cannot parse items" in err and str(unparsable) in err
    bad_src = tmp_path / "no-such-source.md"
    rc, _o, err = run(render, ["skeleton", "--source", str(bad_src),
                               "--out", str(out)])
    assert rc == 1 and "cannot read source" in err and str(bad_src) in err
    no_dir = tmp_path / "no-dir" / "out.md"
    rc, _o, err = run(render, ["skeleton", "--source", str(skel),
                               "--out", str(no_dir)])
    assert rc == 1 and "cannot write output" in err and str(no_dir) in err


def test_argparse_error_exits_2_to_injected_err(mods, capsys):
    """usage/error text goes to the injected err stream (seat LOW)."""
    render, _imp = mods
    out, err = io.StringIO(), io.StringIO()
    rc = render.main(["skeleton"], out=out, err=err)   # required args missing
    assert rc == 2
    assert "usage:" in err.getvalue() and "error:" in err.getvalue()
    assert out.getvalue() == ""
    leaked = capsys.readouterr()
    assert leaked.out == "" and leaked.err == ""


def test_help_goes_to_injected_out(mods, capsys):
    """Same stream-injection sweep for --help (it writes to sys.stdout)."""
    render, _imp = mods
    out, err = io.StringIO(), io.StringIO()
    rc = render.main(["--help"], out=out, err=err)
    assert rc == 0 and "usage:" in out.getvalue()
    assert err.getvalue() == ""
    leaked = capsys.readouterr()
    assert leaked.out == "" and leaked.err == ""


def test_module_documents_tools_on_sys_path_requirement(mods):
    """The bare decisions_import import must state its sys.path requirement."""
    render, _imp = mods
    assert "decisions_import" in render.__doc__
    assert "sys.path" in render.__doc__


def test_tools_path_setup_is_scoped_to_the_test(sys_path_restored, tools_importable):
    """tools/ is on sys.path during the test and gone again afterwards."""
    assert tools_importable in sys.path
