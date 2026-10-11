"""PG-LE-GUARD-KEY: keyed refusals, path exemptions, new detectors.

Synthetic only. Every credential-shaped fixture is built at runtime from
short fragments so no line carries a complete secret shape.
"""
import re

import pytest

from keeper_util import World, defn, seed
from plangraph.keeper.credential_guard import find_shapes, guard, mask
from plangraph.keeper.errors import Refused
from plangraph.keeper.item_fields import check_choices
from plangraph.keeper.item_fixes import check_error_ref


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    seed(world, [("A", [])])
    yield world
    world.close()


# -- runtime-built fragments (each line is clean on its own) --
_OP_HEAD = "docs/incidents/2026-08-30-ups-and-leaked-"
_OP_MID = "password-"
_OP1 = None


def _op1():
    global _OP1
    if _OP1 is None:
        _OP1 = _OP_HEAD + _OP_MID + "x" + ".md"
    return _OP1


def _docker():
    parts = ["server", "media", "downloader", "docker-compose.yml"]
    return "/".join(parts)


def _srcpy():
    parts = ["src", "plangraph", "keeper", "credential_guard.py"]
    return "/".join(parts)


def _b64():
    return "Ab3dEf7h" + "Ij9kLm2nOp4qRs6tUv8wXy0z12"


def _hex():
    return "ab" + "12" * 16


def _shape_user():
    return "us" + "er" + "/" + "Zq9" + "xk2LmPw"


def _shape_svc_md():
    return "sv" + "c" + "/" + "Zq9" + "xk2LmPw" + ".m" + "d"


def _tok_eq():
    return "to" + "ken" + "=" + _b64()


def _sec_colon():
    return "sec" + "ret" + ":" + " " + _b64()


def _api_eq():
    return "ap" + "i_" + "ke" + "y" + "=" + _b64()


def _pwd_eq():
    return "pass" + "wd" + "=" + _b64()


def _pem():
    return "-----BE" + "GIN " + "RSA PRI" + "VATE KEY-----"


def _cert():
    return "-----BE" + "GIN " + "CERTI" + "FICATE-----"


def _kw_login():
    return "log" + "in"


def _kw_password():
    return "pass" + "word"


def _kw_cred():
    return "creden" + "tial"


def _no3(msg, val):
    for i in range(len(val) - 2):
        assert val[i:i + 3] not in msg


def _parse(msg):
    m = re.search(r"at chars (\d+)-(\d+) \((\d+) chars, masked (\*+)\)", msg)
    assert m is not None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)


def _bad_texts():
    return [
        _tok_eq(),
        _sec_colon(),
        _api_eq(),
        _pwd_eq(),
        _kw_password() + " " + _hex(),
        _kw_cred() + " " + _b64(),
        _pem(),
        _kw_password() + " " + _shape_user(),
        _kw_password() + " " + _shape_svc_md(),
    ]


def _bad_vals():
    return [
        _b64(),
        _b64(),
        _b64(),
        _b64(),
        _hex(),
        _b64(),
        "PRIVATE KEY",
        _shape_user(),
        _shape_svc_md().split(".")[0],
    ]


@pytest.mark.parametrize("idx", range(9))
def test_refusals_named_key_field_span(w, idx):
    text = _bad_texts()[idx]
    val = _bad_vals()[idx]
    assert find_shapes(text)
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [defn("X", note=text),
                                             defn("G2", ["X"], kind="goal")])
    assert exc.value.code == "validation"
    assert "demo:X" in exc.value.msg and "note" in exc.value.msg
    a, b, n, stars = _parse(exc.value.msg)
    assert n == b - a and stars == "*" * min(n, 8)
    assert set(stars) == {"*"}
    _no3(str(exc.value), val)
    _no3(exc.value.msg, val)


def test_pem_cert_not_refused():
    assert not find_shapes(_cert() + " " + _kw_login())


def test_pass_paths():
    p1 = _op1()
    p2 = _docker()
    p3 = _srcpy()
    assert not find_shapes(p1)
    assert not find_shapes(_kw_login() + " " + p2)
    assert not find_shapes(_kw_password() + " " + p3)
    assert not find_shapes(_kw_cred() + " " + p2)
    assert not find_shapes("see " + p1 + " for the " + _kw_password())


def test_create_pass_paths(w):
    w.store.create_items(w.p("router"), [
        defn("X", note="see " + _op1() + " for the " + _kw_password()),
        defn("G2", ["X"], kind="goal")])
    assert w.store._item("demo:X") is not None


def test_single_slash_pairs_stay_refused():
    s1 = "adm" + "in/" + "hunt" + "er2abc"
    s2 = "a/" + "bcde" + "fgh.x"
    assert find_shapes(_kw_login() + " " + s1)
    assert find_shapes(_kw_login() + " " + s2)


def test_slash_widening_killed():
    # widening the exemption to every slash token would pass these
    s1 = "adm" + "in/" + "hunt" + "er2abc"
    assert find_shapes(_kw_login() + " " + s1)


def test_mask_exempt_intact_and_secrets_redacted():
    p2 = _docker()
    p1 = _op1()
    t, c = mask(_kw_login() + " " + p2 + " and " + p1)
    assert p2 in t and p1 in t and c == 0
    sec = _shape_user()
    t2, c2 = mask(_kw_login() + " " + sec)
    assert "[redacted]" in t2 and sec not in t2 and c2 == 1
    t3, c3 = mask(_tok_eq())
    assert c3 == 1 and _b64() not in t3
    t4, c4 = mask(_pem() + " here")
    assert c4 == 1 and "PRIVATE KEY" not in t4


def test_multi_item_create_names_right_key(w):
    sec = _kw_password() + " " + _shape_user()
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [
            defn("CLEAN", note="fine"),
            defn("BAD", note=sec),
            defn("G2", ["CLEAN", "BAD"], kind="goal")])
    assert "demo:BAD" in exc.value.msg and "demo:CLEAN" not in exc.value.msg


def test_apply_ops_names_right_inner_key(w):
    sec = _kw_password() + " " + _shape_user()
    with pytest.raises(Refused) as exc:
        w.store.apply_ops(w.p("router"), [{
            "op": "create",
            "item": [defn("C1", note="fine"),
                     defn("C2", note=sec),
                     defn("G2", ["C1", "C2"], kind="goal")]}])
    assert "demo:C2" in str(exc.value)


def test_edit_threads_key(w):
    sec = _kw_password() + " " + _shape_user()
    with pytest.raises(Refused) as exc:
        w.store.edit_definition(w.p("router"), "demo:A", {"note": sec})
    assert "demo:A" in exc.value.msg and "note" in exc.value.msg


def test_import_threads_key(w):
    sec = _kw_password() + " " + _shape_user()
    with pytest.raises(Refused) as exc:
        w.store.import_items(w.p("router"), [defn("X", note=sec),
                                             defn("G2", ["X"], kind="goal")])
    assert "demo:X" in exc.value.msg


def test_folded_unknown_field_threads_key(w):
    sec = _kw_password() + " " + _shape_user()
    with pytest.raises(Refused) as exc:
        w.store.create_items(w.p("router"), [
            defn("X", **{"zz_extra": sec}),
            defn("G2", ["X"], kind="goal")])
    assert "demo:X" in exc.value.msg


def test_choices_label_threads_key():
    sec = _kw_password() + " " + _shape_user()
    lab = {"options": [{"id": "aa", "label": sec},
                       {"id": "bb", "label": "fine choice here"}]}
    with pytest.raises(Refused) as exc:
        check_choices(lab, "question", "demo:Q1")
    assert "demo:Q1" in exc.value.msg


def test_error_ref_threads_key():
    sec = _kw_password() + " " + _shape_user()
    with pytest.raises(Refused) as exc:
        check_error_ref({"code": "E1", "msg": sec}, "demo:E1")
    assert "demo:E1" in exc.value.msg


def test_answer_threads_key(w):
    w.store.create_items(w.p("router"), [
        defn("Q", kind="question", operator=None),
        defn("G", ["Q"], kind="goal")])
    sec = _kw_login() + " " + _shape_user()
    with pytest.raises(Refused) as exc:
        w.store.answer(w.p("router"), "demo:Q", sec, "D-1")
    assert "demo:Q" in exc.value.msg and "answer text" in exc.value.msg


def test_guard_key_none_names_field():
    sec = _kw_password() + " " + _shape_user()
    with pytest.raises(Refused) as exc:
        guard("note", sec)
    assert "note" in exc.value.msg
    a, b, n, stars = _parse(exc.value.msg)
    assert stars == "*" * min(n, 8)
