"""D-374: the credential-shape guard on create/edit/import and the render mask.

Synthetic strings only: ``user/Zq9xk2Lm`` style, never a real credential.
"""
import io
from contextlib import redirect_stderr, redirect_stdout

import pytest

from keeper_util import World, defn, seed
from plangraph.keeper import cli
from plangraph.keeper.credential_guard import find_shapes, mask
from plangraph.keeper.errors import Refused

SECRET = "svcacct/Zq9xk2LmPw"


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


BAD = [f"login with {SECRET} on the box", f"password: {SECRET}",
       f"the CREDENTIAL is {SECRET}", f"pwd={SECRET}", f"{SECRET} passwd",
       f"x{' ' * 100}{SECRET} login"]
OK = ["https://example.org/Zq9xk2LmPw login page",
      "login docs at docs/Zq9xk2LmPw",
      "see src/plangraph/Zq9xk2LmPw for the login code",
      "password reset: tools/resetpass.sh and tools/Zq9xk2LmPw",
      "login uses svc/Zq9xk2Lm.json", "read svc/Zq9xk2LmPw.md for the password",
      "data/Zq9xk2LmPw login", "plain text a/b login", "no keyword svc/Zq9xk2LmPw",
      "login " + "x" * 200 + " svcacct/Zq9xk2LmPw"]


@pytest.mark.parametrize("text", BAD)
def test_shape_next_to_a_credential_word_is_found(text):
    assert find_shapes(text)


@pytest.mark.parametrize("text", OK)
def test_urls_file_paths_and_far_or_keywordless_text_pass(text):
    assert not find_shapes(text)


@pytest.mark.parametrize("field", ["title", "note", "done_criteria",
                                   "description"])
def test_create_refuses_naming_the_field_never_the_value(w, field):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("X", **{field: BAD[0]}),
                                             defn("G2", ["X"], kind="goal")])
    assert exc.value.code == "validation" and field in exc.value.msg
    assert SECRET not in exc.value.msg and "Zq9xk2" not in str(exc.value)
    assert w.store._item("demo:X") is None


def test_edit_refuses_and_apply_ops_rolls_back(w):
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:A", {"note": BAD[1]})
    assert exc.value.code == "validation" and "Zq9xk2" not in str(exc.value)
    with pytest.raises(Refused) as exc2:
        w.store.apply_ops(w.p("router"), [{"op": "create", "item": defn(
            "X", title=BAD[2])}])
    assert "Zq9xk2" not in str(exc2.value)


def test_import_path_refuses_too(w):
    with pytest.raises(Refused):
        w.store.import_items(w.p("router"), [defn("X", note=BAD[3]),
                                             defn("G2", ["X"], kind="goal")])


def test_exceptions_still_create(w):
    w.store.create_items(w.p("router"), [
        defn("X", title=OK[0]), defn("G2", ["X"], kind="goal")])
    assert w.store._item("demo:X")["title"] == OK[0]


def test_mask_replaces_matches_and_counts():
    text, count = mask(f"login {SECRET} and https://h.org/Zq9xk2LmPw login")
    assert text == "login [redacted] and https://h.org/Zq9xk2LmPw login"
    assert count == 1


def test_pg_render_masks_and_counts_on_stderr(w, tmp_path, monkeypatch):
    from keeper_util import World  # noqa: F401
    import json
    snap = w.store.snapshot(w.p("router"))
    snap["items"][0]["note"] = f"login {SECRET}"
    monkeypatch.setattr("plangraph.keeper.cli_verbs.Session.call",
                        lambda self, method, verb, *a, **k: snap)
    out_file = tmp_path / "out.html"
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setenv("PG_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("PG_TOKEN_FILE", str(tmp_path / "t"))
    (tmp_path / "t").write_text("x", encoding="utf-8")
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(["render", "--out", str(out_file)])
    assert code == 0
    html = out_file.read_text(encoding="utf-8")
    assert "[redacted]" in html and "Zq9xk2" not in html
    assert "masked 1" in err.getvalue() and "Zq9xk2" not in err.getvalue()
