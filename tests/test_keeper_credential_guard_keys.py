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


# -- PG-LE-GUARD-KEY fix: F1/F2/F3 regression cases (fragments only) --
def _pair_path_tok():
    return "to" + "ken" + "=" + "/".join(["docs", "setup_guide.md"])


def _pair_path_pwd():
    return "pass" + "word" + ": " + _docker()


def _pair_path_sec():
    return "sec" + "ret" + ": " + "/".join(["docs", "a", "b.md"])


def _pair_lookahead():
    return "sec" + "ret" + ": " + "Ab" + "Cde" + "fghij12" + " (rotate)"


def _prose_texts():
    return [
        "To" + "ken" + ": " + "refresh" + "-after-expiry",
        "sec" + "ret" + ": " + "rotate" + "-quarterly",
        "To" + "ken" + ": " + "Con" + "figured",
        "pass" + "word" + ": " + "Con" + "figuration",
    ]


def _pair_secret_texts():
    return [
        "to" + "ken" + "=" + "Ab" + "Cd" + "1234" + "Efgh",
        "sec" + "ret" + ": " + "kJ" + "8sd" + "9Qw" + "2xLm",
        "ap" + "i_" + "ke" + "y" + "=" + "Qx" + "7Zp" + "Lm2" + "vB9r",
        "to" + "ken" + "=" + "Ab" + "Cd" + "EfGh" + "IjKl",
    ]


def _pair_secret_vals():
    return [
        "Ab" + "Cd" + "1234" + "Efgh",
        "kJ" + "8sd" + "9Qw" + "2xLm",
        "Qx" + "7Zp" + "Lm2" + "vB9r",
        "Ab" + "Cd" + "EfGh" + "IjKl",
    ]


def _chunk_bad_texts():
    h2 = "hunt" + "er2"
    s1234 = "secr" + "et1234"
    return [
        _kw_login() + " " + "/".join(["a", "b", h2]),
        _kw_login() + " " + "/".join(["a", "b", h2 + ".m" + "d"]),
        _kw_login() + " " + "/".join(["a", "b", s1234 + ".m" + "d"]),
    ]


def _chunk_bad_vals():
    h2 = "hunt" + "er2"
    s1234 = "secr" + "et1234"
    return [h2, h2, s1234]


def _chunk_pass_text():
    return _kw_password() + " " + "/".join(
        ["Server", "Media", "Downloader" + "_v2", "docker-compose.yml"])


def test_pair_path_values_still_refused():
    for text in (_pair_path_tok(), _pair_path_pwd(), _pair_path_sec(),
                 _pair_lookahead()):
        assert find_shapes(text), text
        with pytest.raises(Refused):
            guard("note", text)
        t, c = mask(text)
        assert c == 1 and "[redacted]" in t


def test_pair_prose_values_pass():
    for text in _prose_texts():
        assert not find_shapes(text), text
        guard("note", text)
        t, c = mask(text)
        assert c == 0 and t == text


def test_pair_digit_or_interior_upper_refused():
    for text in _pair_secret_texts():
        assert find_shapes(text), text
        with pytest.raises(Refused):
            guard("note", text)
        t, c = mask(text)
        assert c >= 1 and "[redacted]" in t


def test_chunk_secret_refused_and_paths_pass():
    for text in _chunk_bad_texts():
        assert find_shapes(text), text
        with pytest.raises(Refused):
            guard("note", text)
    assert not find_shapes(_chunk_pass_text())
    guard("note", _chunk_pass_text())
    t, c = mask(_chunk_pass_text())
    assert c == 0
    assert not find_shapes(_kw_login() + " " + _docker())
    assert not find_shapes(_kw_login() + " " + _op1())


def test_guard_mask_agree_and_no_trigram_leak():
    cases = [
        (_pair_path_tok(), True, None),
        (_pair_path_pwd(), True, None),
        (_pair_path_sec(), True, None),
        (_pair_lookahead(), True, "Ab" + "Cde" + "fghij12"),
    ]
    for text, val in zip(_pair_secret_texts(), _pair_secret_vals()):
        cases.append((text, True, val))
    chunk_texts = _chunk_bad_texts()
    chunk_vals = _chunk_bad_vals()
    for i, text in enumerate(chunk_texts):
        # "secr"+"et1234" shares trigrams (e.g. "cre") with the refusal
        # boilerplate ("credential"), so the no-trigram leak check applies
        # to the hunter2 pair and the pair secrets, not to it; agreement
        # is still checked for every case.
        val = chunk_vals[i] if i < 2 else None
        cases.append((text, True, val))
    for text in _prose_texts():
        cases.append((text, False, None))
    cases.append((_chunk_pass_text(), False, None))
    for text, refused, val in cases:
        try:
            guard("note", text)
            was_refused = False
            msg = ""
        except Refused as exc:
            was_refused = True
            msg = str(exc) + exc.msg
        assert was_refused == refused, text
        t, c = mask(text)
        assert (c >= 1) == refused, text
        assert ("[redacted]" in t) == refused, text
        if refused and val is not None:
            _no3(msg, val)
            _no3(t, val)


def _quoted_value():
    return "q7Zx" + "9mK2" + "pLw"


@pytest.mark.parametrize("template", [
    'password: "{v}"', "password = '{v}'", '{{"token": "{v}"}}',
    "secret: '{v}' (rotate)", 'api_key="{v}"'])
def test_quoted_pair_values_are_refused_and_masked(template):
    val = _quoted_value()
    text = template.format(v=val)
    with pytest.raises(Refused) as exc:
        guard("note", text)
    _no3(exc.value.msg, val)
    masked, count = mask(text)
    assert count >= 1
    _no3(masked, val)


_W = "log" + "in "
_SPLIT = [_W + "a/b/" + "abc1" + "_def2", _W + "a/b/" + "abc1" + "_def2.txt",
          _W + "a/b/" + "aB1c" + "_dE2f.txt",
          "pass" + "word x/y/" + "ab1_" + "cd2_" + "ef3.md"]


@pytest.mark.parametrize("text", _SPLIT)
def test_split_short_mixed_chunks_are_one_secret(text):
    with pytest.raises(Refused):
        guard("note", text)
    assert mask(text)[1] >= 1


_NUMRUN = [_W + "docs/incidents/" + "AbCdEf" + "-123456.md",
           _W + "docs/incidents/" + "abc" + "-123456.md"]


@pytest.mark.parametrize("text", _NUMRUN)
def test_bare_number_run_chunk_is_secret_like(text):
    with pytest.raises(Refused):
        guard("note", text)
    assert mask(text)[1] >= 1


def test_find_shapes_hit_exposes_group_text():
    text = "pass" + "word user/" + "Zq9xk2" + "LmPw"
    hits = find_shapes(text)
    assert hits and hits[0].group() == text[hits[0].start():hits[0].end()]
    assert hits[0].group(0) == hits[0].group()


@pytest.mark.parametrize("pair", ["tok" + "en=hunt" + "er2xyz", "sec" + "ret: abc" + "12345"])
def test_pair_value_with_digit_but_no_capital_is_refused(pair):
    with pytest.raises(Refused):
        guard("note", pair)
    assert mask(pair)[1] >= 1


@pytest.mark.parametrize("text", [
    "login docs/runbook/docker-compose-v2.yml",
    "password server/media/downloader-v1/notes.md",
    "login docs/2026-08-30-ups-notes/readme.md"])
def test_version_and_single_mixed_chunks_stay_paths(text):
    guard("note", text)
    assert mask(text) == (text, 0)


@pytest.mark.parametrize("text", [
    'password: "rotate-soon"', "token: 'Configured'",
    'tokens["w"] = 1'])
def test_quoted_prose_is_not_refused(text):
    guard("note", text)
    assert mask(text) == (text, 0)
