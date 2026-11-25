"""planner pack under the admission leg cap: first pack, retry prefix and the gate.

Split from test_planner_service.py (size); the fixtures come from there.
"""
import pytest

from keeper_util import seed
from plangraph.planner import service
from plangraph.planner.legs import Leg
from test_planner_service import gate, put, repo, w  # noqa: F401  (fixtures and helpers)

#: Declared request limits on a 1m window class (the class pack budget alone is far above).
GROK_LEG = Leg(id="grok-120k", tier="free", harness="opencode", model="grok-model",
               family="grok", windows=("1m",), max_request_tokens=120000,
               trains_on_data=False)
SHIPPED_200K = Leg(id="shipped-200k", tier="free", harness="opencode", model="shipped-model",
                   family="shipped", windows=("1m",), max_request_tokens=200000,
                   trains_on_data=False)
WIDE_LEG = Leg(id="wide-free", tier="free", harness="opencode", model="wide-model",
               family="wide", windows=("1m",), max_request_tokens=1000000,
               trains_on_data=False)
LEG_LIMIT = 120000
BIG_TAIL = "\n".join("E" * 400 for _ in range(60))


def _big_write_set(repo, tokens):
    names, left, i = [], tokens, 0
    while left > 0:
        n = min(left, 19000)
        name = f"src/big{i}.py"
        (repo / name).write_text("x" * (4 * n), encoding="utf-8")
        names.append(name)
        left -= n
        i += 1
    return names


def test_card_that_fits_the_leg_only_without_a_tail_is_refused_at_the_gate(w, repo):
    key, key2 = seed(w, [("A", []), ("B", [])])
    put(w, key, window_class="1m", estimate_tokens=1000,
        write_set=_big_write_set(repo, LEG_LIMIT - 300))
    refused = gate(w, repo, key, legs=(GROK_LEG,))
    assert refused.state == "needs_input"
    assert any(f.code == "leg_request_too_big" for f in refused.verdict.findings)

    put(w, key2, window_class="1m", estimate_tokens=1000,
        write_set=_big_write_set(repo, LEG_LIMIT - 4000))
    admitted = gate(w, repo, key2, legs=(GROK_LEG,))
    assert admitted.ready and admitted.state == "ready"
    first = service.pack_for_card(w.store, w.p("router"), key2, repo_root=repo,
                                  max_request_tokens=LEG_LIMIT)
    retry = service.pack_for_card(w.store, w.p("router"), key2, repo_root=repo,
                                  max_request_tokens=LEG_LIMIT, check_tail=BIG_TAIL)
    assert retry.text[:retry.prefix_chars] == first.text
    assert retry.tokens <= LEG_LIMIT


def test_shipped_leg_default_first_pack_equals_the_retry_prefix(w, repo):
    """An admitted card's first pack (with or without the route) never exceeds the leg cap and
    is the prefix of the retry; a card whose full pack plus tail would not fit is refused."""
    key, key2, key3 = seed(w, [("A", []), ("B", []), ("C", [])])
    (repo / "src" / "read.py").write_text("r" * 4 * 5000, encoding="utf-8")
    put(w, key, window_class="1m", estimate_tokens=1000, read_set=["src/read.py"],
        write_set=_big_write_set(repo, 185000))
    admitted = gate(w, repo, key, legs=(SHIPPED_200K,))
    assert admitted.ready and admitted.route.first_leg.max_request_tokens == 200000
    default_first = service.pack_for_card(w.store, w.p("router"), key, repo_root=repo)
    routed_first = service.pack_for_card(w.store, w.p("router"), key, repo_root=repo,
                                         route=admitted.route)
    for retry in (
        service.pack_for_card(w.store, w.p("router"), key, repo_root=repo,
                              check_tail=BIG_TAIL),
        service.pack_for_card(w.store, w.p("router"), key, repo_root=repo,
                              route=admitted.route, check_tail=BIG_TAIL),
    ):
        assert default_first.tokens <= 200000 and retry.tokens <= 200000
        assert retry.text[:retry.prefix_chars] == default_first.text == routed_first.text
        assert [f.path for f in retry.files] == [f.path for f in default_first.files]
        assert retry.skipped == default_first.skipped == ()
    # full pack over the cap (as the pinned base refused) and full pack inside the cap but
    # without room for the tail are both refused at admission
    (repo / "src" / "read2.py").write_text("r" * 4 * 19000, encoding="utf-8")
    put(w, key2, window_class="1m", estimate_tokens=1000, read_set=["src/read.py", "src/read2.py"],
        write_set=_big_write_set(repo, 185000))
    assert not gate(w, repo, key2, legs=(SHIPPED_200K,)).ready
    put(w, key3, window_class="1m", estimate_tokens=1000, read_set=["src/read.py"],
        write_set=_big_write_set(repo, 194000))
    tight = gate(w, repo, key3, legs=(SHIPPED_200K,))
    assert not tight.ready
    assert any("tail reserve" in f.message for f in tight.verdict.findings)


def test_retry_without_the_route_stays_in_the_cap_of_an_admitted_card(w, repo):
    """Admission excluded a wider leg; a retry packed without the route still fits the cap."""
    key, = seed(w, [("A", [])])
    put(w, key, window_class="1m", estimate_tokens=1000,
        write_set=_big_write_set(repo, LEG_LIMIT - 4000))
    admitted = gate(w, repo, key, legs=(WIDE_LEG, GROK_LEG), unavailable=("wide-free",))
    assert admitted.ready and admitted.route.first_leg.id == "grok-120k"
    for kw in ({"route": admitted.route}, {}):
        retry = service.pack_for_card(w.store, w.p("router"), key, repo_root=repo,
                                      check_tail=BIG_TAIL, **kw)
        assert retry.tokens <= LEG_LIMIT
