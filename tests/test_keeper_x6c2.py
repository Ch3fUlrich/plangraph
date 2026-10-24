"""X6c2: normalised family names must be a plain ``[a-z0-9._-]+`` token.

A non-blank family that fails the charset is refused at both write entry points
(``auth.add_principal`` and the store's create/import owner_family), never
silently coerced to unknown.  Blank/``None`` stays allowed and means unknown.
``norm_family`` on read is untouched, so legacy rows keep working.
"""
import pytest

from keeper_util import World, defn
from plangraph.keeper import auth
from plangraph.keeper.errors import Refused

BAD = [
    "team\u200bC",   # zero-width space inside
    "ＡＢＣ",         # full-width letters
    "team c",        # embedded space
    "ß",             # sharp s survives casefold, not ASCII
]
MESSAGE = "family must match [a-z0-9._-]+"


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def _assert_refused(exc):
    assert isinstance(exc.value, Refused)
    assert exc.value.code == "validation"
    assert exc.value.msg == MESSAGE


# ------------------------------------------------------------ add_principal

@pytest.mark.parametrize("family", BAD)
def test_add_principal_refuses_bad_family(w, family):
    with pytest.raises(Refused) as exc:
        auth.add_principal(w.conn, "rev", "reviewer", ["demo"], family=family)
    _assert_refused(exc)


def test_add_principal_stores_normalised_family(w):
    auth.add_principal(w.conn, "rev", "reviewer", ["demo"], family="Anthropic ")
    stored = w.conn.execute(
        "SELECT family FROM principals WHERE name='rev'").fetchone()[0]
    assert stored == "anthropic"


@pytest.mark.parametrize("family", ["", "   ", None])
def test_add_principal_allows_blank_family(w, family):
    auth.add_principal(w.conn, "rev", "reviewer", ["demo"], family=family)
    stored = w.conn.execute(
        "SELECT family FROM principals WHERE name='rev'").fetchone()[0]
    assert stored is None


# ------------------------------------------------------------- create_items

@pytest.mark.parametrize("family", BAD)
def test_create_items_refuses_bad_owner_family(w, family):
    item = defn("A")
    item["owner_family"] = family
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [item])
    _assert_refused(exc)


def _with_goal(lane):
    """``_validate_change`` refuses work with no reachable goal, so pair one."""
    return [lane, defn("GOAL", ["A"], kind="goal")]


def test_create_items_stores_normalised_owner_family(w):
    w.store.create_items(
        w.p("router"), _with_goal(defn("A", owner_family="Anthropic ")))
    stored = w.conn.execute(
        "SELECT owner_family FROM items WHERE key='demo:A'").fetchone()[0]
    assert stored == "anthropic"


@pytest.mark.parametrize("family", ["", None])
def test_create_items_allows_blank_owner_family(w, family):
    w.store.create_items(
        w.p("router"), _with_goal(defn("A", owner_family=family)))
    stored = w.conn.execute(
        "SELECT owner_family FROM items WHERE key='demo:A'").fetchone()[0]
    assert stored is None


# ------------------------------------------------------------- import_items

@pytest.mark.parametrize("family", BAD)
def test_import_items_refuses_bad_owner_family(w, family):
    item = defn("A", state="open")
    item["owner_family"] = family
    with pytest.raises(Refused) as exc:
        w.store.import_items(w.p("router"), [item])
    _assert_refused(exc)
