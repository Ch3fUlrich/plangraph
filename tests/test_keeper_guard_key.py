"""PL-33: the credential refusal names the item key; paths near a credential word stay refused (D-388, D-1558).

Synthetic strings only: ``admin/hunter22`` style, never a real credential.
The message format is ``[<key>: ]<field> looks like it contains a credential at
chars <start>-<end> (<first 2 chars>*** (len <n>)) ; remove it`` -- the key is a
normal ``project:ID`` item key, and the matched span is never echoed.
"""
import pytest

from keeper_util import World, defn, seed
from plangraph.keeper.credential_guard import find_shapes, guard, mask, mask_all
from plangraph.keeper.errors import Refused

SECRET = "admin/hunter2secret"

#: path-like text near a credential keyword: NO exemption (D-388), still refused.
PATHS = [
    "see docs/plan/manual-triage-2026-10-02.md for login steps",
    "server:MT-003 uses ops/runbooks/login-flow.md",
    "credential rotation per docs/briefs/MT-095-notes.md",
    "src/plangraph/keeper/credential_guard.py password check",
]

#: credential pairs (plus one beside a path) -> refused.
REAL = [
    "login admin/hunter2secret",
    "password: svc/Xk9fT2mQ",
    "credentials bob/correcthorse",
    "password for docs/readme.md is admin/swordfish99",
    f"login see {PATHS[0]} then {SECRET}",
]

LEAKS = ("hunter2secret", "Xk9fT2mQ", "correcthorse", "swordfish99")


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


def _masked(text):
    """The only fragment of the matched span the message may show."""
    span = find_shapes(text)[0]
    return (f"{text[span.start():span.start() + 2]}***"
            f" (len {span.end() - span.start()})")


# ------------------------------------------------ message: key + field + span
def test_guard_message_is_exactly_key_field_and_masked_span():
    text = "login with " + SECRET
    start, end = text.index(SECRET), text.index(SECRET) + len(SECRET)
    masked = f"{SECRET[:2]}*** (len {len(SECRET)})"
    with pytest.raises(Refused) as exc:
        guard("note", text, "demo:C")
    assert exc.value.code == "validation"
    assert exc.value.msg == (
        f"demo:C: note looks like it contains a credential at chars "
        f"{start}-{end} ({masked}) ; remove it")
    assert masked in exc.value.msg
    assert SECRET not in exc.value.msg and "hunter2" not in exc.value.msg


def test_guard_without_a_known_key_has_no_prefix():
    text = "password: " + SECRET
    with pytest.raises(Refused) as exc:
        guard("title", text)
    assert exc.value.msg.startswith(
        "title looks like it contains a credential at chars ")
    assert _masked(text) in exc.value.msg and SECRET not in exc.value.msg


def test_create_refusal_names_the_item_key_and_never_the_secret(w):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [
            defn("P", note=f"login {SECRET}"), defn("G2", ["P"], kind="goal")])
    msg = exc.value.msg
    assert exc.value.code == "validation"
    assert "demo:P: note looks like it contains a credential at chars" in msg
    assert _masked(f"login {SECRET}") in msg
    assert SECRET not in msg and "hunter2" not in msg
    assert w.store._item("demo:P") is None


def test_folded_unknown_field_refusal_names_the_item_key(w):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [
            defn("C", intent=f"login {SECRET}"),
            defn("G2", ["C"], kind="goal")])
    assert "demo:C: intent looks like it contains a credential" in exc.value.msg
    assert SECRET not in exc.value.msg


def test_edit_refusal_names_the_edited_key(w):
    before = w.store._item("demo:A")["note"]
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:A",
                                {"note": f"password {SECRET}"})
    msg = exc.value.msg
    assert "demo:A: note looks like it contains a credential at chars" in msg
    assert _masked(f"password {SECRET}") in msg
    assert SECRET not in msg and "hunter2" not in msg
    assert w.store._item("demo:A")["note"] == before


def test_batch_position_three_is_named_and_the_op_prefix_survives(w):
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), [
            {"op": "create", "item": defn("B1")},
            {"op": "create", "item": defn("B2")},
            {"op": "create", "item": defn("C", note=f"login {SECRET}")},
            {"op": "create", "item": defn("B3")}])
    msg = exc.value.msg
    assert msg.startswith("op 3 create demo:C: validation ")
    assert "demo:C: note looks like it contains a credential at chars" in msg
    assert _masked(f"login {SECRET}") in msg
    assert SECRET not in msg and "hunter2" not in msg
    for rolled in ("demo:B1", "demo:B2", "demo:C", "demo:B3"):
        assert w.store._item(rolled) is None


# --------------------------------------------------- paths stay refused (D-388)
@pytest.mark.parametrize("text", PATHS)
def test_path_like_tokens_near_a_credential_word_are_still_shapes(text):
    assert find_shapes(text)
    assert mask(text)[1] >= 1


@pytest.mark.parametrize("text", PATHS)
def test_create_refuses_a_path_near_a_credential_word_naming_the_key(w, text):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [
            defn("P", note=text), defn("G2", ["P"], kind="goal")])
    assert "demo:P: note looks like it contains a credential" in exc.value.msg
    assert w.store._item("demo:P") is None


# --------------------------------------------------- real secrets still fail
@pytest.mark.parametrize("text", REAL)
def test_real_secrets_are_still_refused(text):
    assert find_shapes(text)


@pytest.mark.parametrize("text", REAL)
def test_create_refuses_real_secrets_naming_the_key(w, text):
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [
            defn("P", note=text), defn("G2", ["P"], kind="goal")])
    msg = exc.value.msg
    assert exc.value.code == "validation"
    assert "demo:P: note looks like it contains a credential" in msg
    assert _masked(text) in msg
    assert SECRET not in msg and not any(leak in msg for leak in LEAKS)
    assert w.store._item("demo:P") is None


# --------------------------------------------------- mask stays consistent
def test_mask_redacts_exactly_the_set_find_shapes_returns():
    mixed = f"login see {PATHS[0]} then {SECRET}"
    hits = find_shapes(mixed)
    assert len(hits) == 2 and mixed[hits[-1].start():hits[-1].end()] == SECRET
    text, count = mask(mixed)
    assert count == len(hits)
    assert text.endswith("then [redacted]") and SECRET not in text


def test_mask_all_counts_the_same_matches_as_find_shapes():
    value = {"note": f"password {SECRET}", "path": PATHS[1], "n": 3}
    assert len(find_shapes(value["note"])) == 1
    assert len(find_shapes(value["path"])) == 1
    masked, count = mask_all(value)
    assert count == 2 and SECRET not in masked["note"]
    assert "[redacted]" in masked["path"] and masked["n"] == 3


@pytest.mark.parametrize("gap", [121, 200])
def test_more_than_120_chars_between_word_and_shape_still_passes(gap):
    assert find_shapes(f"login {'x' * gap} {SECRET}") == []
