"""planner.card: schema, validation, batch import, strip, load and apply."""
import copy

import pytest

from plangraph.planner.card import (Card, CardError, apply_cards, cards_from_batch,
                                    load_cards_yaml, parse_card, strip_cards)


def raw(**kw):
    base = {"outcome": "add the thing", "write_set": ["src/a.py"], "read_set": ["src/b.py"],
            "check_cmd": "pytest -q", "window_class": "128k", "risk_tags": ["migration"],
            "estimate_tokens": 40000}
    base.update(kw)
    return base


def problems_of(card_raw):
    with pytest.raises(CardError) as info:
        parse_card(card_raw, "demo:A")
    assert info.value.key == "demo:A"
    return info.value.problems


# ------------------------------------------------------------------- parse_card
def test_valid_roundtrip_to_store():
    card = parse_card(raw())
    assert card == Card.from_dict(raw())
    out = card.to_store()
    assert set(out) == {"outcome", "read_set", "write_set", "exports", "forbid_paths",
                        "smoke_checks", "check_cmd", "heldout_check", "window_class",
                        "risk_tags", "estimate_tokens"}
    assert "check_kind" not in out
    assert out["write_set"] == ["src/a.py"] and isinstance(out["write_set"], list)
    assert out["exports"] == [] and out["heldout_check"] is None
    assert parse_card(out) == card


def test_card_is_frozen():
    with pytest.raises(Exception):
        parse_card(raw()).outcome = "x"


def test_check_kind_default_and_roundtrip():
    assert parse_card(raw()).check_kind == "change"
    assert Card(outcome="old card").check_kind == "change"
    for kind in ("change", "regression"):
        card = parse_card(raw(check_kind=kind))
        assert card.check_kind == kind
        if kind == "change":
            assert "check_kind" not in card.to_store()
        else:
            assert card.to_store()["check_kind"] == "regression"
        assert parse_card(card.to_store()) == card


@pytest.mark.parametrize("kind", [None, "", "other", " regression ", 1, []])
def test_invalid_check_kind(kind):
    assert any("check_kind" in p for p in problems_of(raw(check_kind=kind)))


def test_unknown_field_rejected():
    (p,) = problems_of(raw(bogus=1))
    assert "unknown card fields" in p and "bogus" in p


def test_outcome_required_and_bounded():
    assert any("outcome is required" in p for p in problems_of({}))
    assert any("outcome" in p for p in problems_of(raw(outcome="   ")))
    assert any("outcome" in p for p in problems_of(raw(outcome=3)))
    assert any("exceeds 2000" in p for p in problems_of(raw(outcome="x" * 2001)))


def test_bad_window_class():
    (p,) = problems_of(raw(window_class="2m"))
    assert "window_class" in p


def test_bad_risk_tag():
    (p,) = problems_of(raw(risk_tags=["auth", "nope"]))
    assert "risk_tags" in p and "nope" in p


def test_bool_and_negative_estimate_rejected():
    assert any("estimate_tokens" in p for p in problems_of(raw(estimate_tokens=True)))
    assert any("estimate_tokens" in p for p in problems_of(raw(estimate_tokens=-1)))
    assert any("estimate_tokens" in p for p in problems_of(raw(estimate_tokens=1.5)))
    assert parse_card(raw(estimate_tokens=0)).estimate_tokens == 0


def test_dotdot_and_absolute_paths_rejected():
    for field in ("read_set", "write_set", "forbid_paths"):
        for bad in ("../x.py", "a/../b.py", "a\\..\\b", "/etc/x", "C:/x", "c:\\x"):
            ps = problems_of(raw(**{field: [bad]}))
            assert len(ps) == 1 and field in ps[0], (field, bad, ps)


def test_paths_normalised_and_deduped():
    card = parse_card(raw(write_set=[" ./src\\a.py ", "src/a.py", "./b/c.py", "b/c.py"]))
    assert card.write_set == ("src/a.py", "b/c.py")
    assert parse_card(raw(read_set=["x", "x", "y"])).read_set == ("x", "y")


def test_dedup_keeps_order_for_non_path_lists():
    card = parse_card(raw(smoke_checks=["b", "a", "b"], exports=["z", "z"],
                          risk_tags=["money", "auth", "money"]))
    assert card.smoke_checks == ("b", "a")
    assert card.exports == ("z",)
    assert card.risk_tags == ("money", "auth")


def test_non_path_lists_not_path_checked():
    assert parse_card(raw(exports=["../weird"])).exports == ("../weird",)


def test_list_shape_rules():
    assert any("write_set" in p for p in problems_of(raw(write_set="src/a.py")))
    assert any("entries" in p for p in problems_of(raw(write_set=[""])))
    assert any("entries" in p for p in problems_of(raw(write_set=[3])))
    assert any("entries" in p for p in problems_of(raw(exports=["x" * 501])))
    assert any("at most 500" in p for p in problems_of(raw(exports=[str(i) for i in range(501)])))
    assert parse_card(raw(write_set=("a", "b"))).write_set == ("a", "b")


def test_text_fields():
    assert any("check_cmd" in p for p in problems_of(raw(check_cmd=5)))
    assert any("heldout_check" in p for p in problems_of(raw(heldout_check="x" * 2001)))
    card = parse_card(raw(check_cmd=None, heldout_check="pytest tests/h"))
    assert card.check_cmd is None and card.heldout_check == "pytest tests/h"


def test_all_problems_collected():
    ps = problems_of({"bogus": 1, "window_class": "9k", "risk_tags": ["x"],
                      "estimate_tokens": True, "write_set": ["../a"], "check_cmd": 1})
    joined = " | ".join(ps)
    for needle in ("outcome", "unknown", "window_class", "risk_tags", "estimate_tokens",
                   "write_set", "check_cmd"):
        assert needle in joined, needle
    assert len(ps) >= 7


def test_non_mapping_card():
    assert problems_of([1]) == ["card must be an object"]


# -------------------------------------------------------------- cards_from_batch
def test_cards_mapping_qualified_and_bare():
    doc = {"project": "demo", "cards": {"demo:A": raw(), "B": raw(outcome="b")}}
    cards = cards_from_batch(doc)
    assert set(cards) == {"demo:A", "demo:B"}
    assert cards["demo:B"].outcome == "b"
    assert set(cards_from_batch({"cards": {"B": raw()}}, default_project="other")) == {
        "other:B"}


def test_bare_id_without_project_rejected():
    with pytest.raises(CardError) as info:
        cards_from_batch({"cards": {"B": raw()}})
    assert "no project" in str(info.value)


def test_bad_card_in_batch_names_key():
    with pytest.raises(CardError) as info:
        cards_from_batch({"cards": {"demo:A": raw(window_class="x")}})
    assert info.value.key == "demo:A" and info.value.problems


def test_inline_cards_in_ops_dict_and_list():
    doc = {"ops": [
        {"op": "create", "item": {"project": "demo", "id": "A", "card": raw()}},
        {"op": "create", "item": [
            {"project": "demo", "id": "B", "card": raw(outcome="b")},
            {"project": "demo", "id": "C"},
            {"project": "demo", "id": "D", "key": "other:D", "card": raw(outcome="d")}]},
        {"op": "set_kind", "key": "demo:A", "kind": "question"},
    ]}
    cards = cards_from_batch(doc)
    assert set(cards) == {"demo:A", "demo:B", "other:D"}


def test_inline_cards_in_items_list_and_bare_list():
    items = [{"project": "demo", "id": "A", "card": raw()}, {"project": "demo", "id": "B"}]
    assert set(cards_from_batch({"items": items})) == {"demo:A"}
    assert set(cards_from_batch(items)) == {"demo:A"}
    assert set(cards_from_batch({"project": "demo", "items": [{"id": "Z", "card": raw()}]})
               ) == {"demo:Z"}


def test_both_forms_together_and_no_cards():
    doc = {"cards": {"demo:A": raw()},
           "items": [{"project": "demo", "id": "B", "card": raw()}]}
    assert set(cards_from_batch(doc)) == {"demo:A", "demo:B"}
    assert cards_from_batch({"ops": []}) == {}


def test_duplicate_card_rejected():
    doc = {"cards": {"demo:A": raw()},
           "items": [{"project": "demo", "id": "A", "card": raw()}]}
    with pytest.raises(CardError) as info:
        cards_from_batch(doc)
    assert info.value.key == "demo:A" and "duplicate" in info.value.problems[0]
    twice = {"items": [{"project": "demo", "id": "A", "card": raw()},
                       {"project": "demo", "id": "A", "card": raw()}]}
    with pytest.raises(CardError):
        cards_from_batch(twice)
    both = {"project": "demo", "cards": {"demo:A": raw(), "A": raw()}}
    with pytest.raises(CardError):
        cards_from_batch(both)


def test_malformed_documents():
    for bad in ("text", 3, None):
        with pytest.raises(CardError):
            cards_from_batch(bad)
    with pytest.raises(CardError):
        cards_from_batch({"cards": [1]})
    with pytest.raises(CardError):
        cards_from_batch({"items": [{"card": raw()}]})


# -------------------------------------------------------------------- strip_cards
def test_strip_cards_removes_and_does_not_mutate():
    doc = {"batch": "b", "project": "demo",
           "cards": {"demo:X": raw()},
           "items": [{"project": "demo", "id": "A", "card": raw()}],
           "ops": [{"op": "create", "item": {"project": "demo", "id": "B", "card": raw()}},
                   {"op": "create", "item": [{"project": "demo", "id": "C", "card": raw()}]}]}
    before = copy.deepcopy(doc)
    out = strip_cards(doc)
    assert doc == before
    assert "cards" not in out
    assert out["items"] == [{"project": "demo", "id": "A"}]
    assert out["ops"][0]["item"] == {"project": "demo", "id": "B"}
    assert out["ops"][1]["item"] == [{"project": "demo", "id": "C"}]
    assert cards_from_batch(out) == {}
    out["items"][0]["x"] = 1
    assert "x" not in doc["items"][0]


def test_strip_cards_bare_list():
    items = [{"project": "demo", "id": "A", "card": raw()}]
    assert strip_cards(items) == [{"project": "demo", "id": "A"}]
    assert "card" in items[0]


# ---------------------------------------------------------------- load_cards_yaml
def test_load_cards_yaml(tmp_path):
    path = tmp_path / "batch.yaml"
    path.write_text(
        "project: demo\n"
        "cards:\n"
        "  A:\n"
        "    outcome: do a\n"
        "    write_set: [./src/a.py]\n"
        "    risk_tags: [auth]\n"
        "    estimate_tokens: 1000\n"
        "ops:\n"
        "  - op: create\n"
        "    item:\n"
        "      project: demo\n"
        "      id: B\n"
        "      card: {outcome: do b, window_class: 256k}\n",
        encoding="utf-8")
    cards = load_cards_yaml(path)
    assert set(cards) == {"demo:A", "demo:B"}
    assert cards["demo:A"].write_set == ("src/a.py",)
    assert cards["demo:A"].risk_tags == ("auth",)
    assert cards["demo:B"].window_class == "256k"


def test_load_cards_yaml_invalid(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("cards:\n  demo:A: {outcome: x, window_class: 9k}\n", encoding="utf-8")
    with pytest.raises(CardError):
        load_cards_yaml(path)


# ------------------------------------------------------------------- apply_cards
class FakeStore:
    def __init__(self):
        self.calls = []

    def put_card(self, principal, key, card):
        self.calls.append((principal, key, card))
        return {"key": key}


def test_apply_cards_sorted_and_returns_keys():
    store = FakeStore()
    cards = {"demo:B": parse_card(raw(outcome="b")), "demo:A": parse_card(raw())}
    assert apply_cards(store, "me", cards) == ["demo:A", "demo:B"]
    assert [c[1] for c in store.calls] == ["demo:A", "demo:B"]
    assert all(c[0] == "me" for c in store.calls)
    assert store.calls[0][2] == cards["demo:A"].to_store()
    assert apply_cards(FakeStore(), "me", {}) == []


def test_apply_cards_real_store_roundtrip(tmp_path):
    keeper_util = pytest.importorskip("keeper_util")
    world = keeper_util.World(tmp_path)
    try:
        (key,) = keeper_util.seed(world, [("A", [])])
        card = parse_card(raw(exports=["f"], smoke_checks=["s"], forbid_paths=["docs"]))
        assert "check_kind" not in card.to_store()
        assert apply_cards(world.store, world.p("router"), {key: card}) == [key]
        got = world.store.get_card(world.p("reader"), key)
        want = card.to_store()
        assert {k: got[k] for k in want} == want
    finally:
        world.close()


# ------------------------------------------------------- F2 explicit key qualification
def test_explicit_bare_key_qualified_with_project():
    doc = {"project": "alpha", "items": [{"key": "42", "card": raw()}]}
    assert set(cards_from_batch(doc)) == {"alpha:42"}
    doc = {"project": "alpha", "items": [{"key": "42", "project": "beta", "card": raw()}]}
    assert set(cards_from_batch(doc)) == {"beta:42"}
    doc = {"items": [{"key": "42", "card": raw()}]}
    assert set(cards_from_batch(doc, default_project="gamma")) == {"gamma:42"}
    assert set(cards_from_batch({"items": [{"key": "p:42", "card": raw()}]})) == {"p:42"}


def test_explicit_bare_key_without_project_rejected():
    with pytest.raises(CardError):
        cards_from_batch({"items": [{"key": "42", "card": raw()}]})


# ------------------------------------------------------- F3 credential guard in parse_card
def _secret():
    return "/".join(["svcacct", "Zq9xk2" + "LmPw"])


def test_credential_shapes_collected_as_problems():
    bad = f"login with {_secret()} on the box"
    ps = problems_of(raw(outcome=bad, check_cmd=bad, heldout_check=bad,
                         read_set=["ok.py", bad], smoke_checks=[bad], exports=[bad]))
    joined = " ".join(ps)
    for name in ("outcome", "check_cmd", "heldout_check", "read_set", "smoke_checks",
                 "exports"):
        assert any(p.startswith(name) and "credential" in p for p in ps), (name, ps)
    assert _secret() not in joined
    assert len(ps) >= 6


def test_plain_text_passes_credential_guard():
    assert parse_card(raw(outcome="login page svc/x")).outcome == "login page svc/x"


# ------------------------------------------------------- F4 path spellings
def test_path_spellings_collapse():
    for spelling in ("src//", "src/", "src", "./src/.", "src/./", "src//./", "./src"):
        assert parse_card(raw(write_set=[spelling])).write_set == ("src",), spelling
    assert parse_card(raw(write_set=["a//b/./c.py"])).write_set == ("a/b/c.py",)
    assert parse_card(raw(write_set=["src//", "src/", "src"])).write_set == ("src",)
    assert problems_of(raw(write_set=["./."]))
