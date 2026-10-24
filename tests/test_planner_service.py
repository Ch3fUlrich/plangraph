"""planner.service: gate_card, running_write_sets, route/pack glue and batch import.

Portable: ``World`` follows ``PG_KEEPER_BACKEND``, rows are read back through the store
or ``conn.execute`` only.  Repo trees live under ``tmp_path``; nothing is executed.
"""
import pytest

from keeper_util import World, seed
from plangraph.planner import service
from plangraph.planner.legs import DEFAULT_LEGS, Leg

#: One tiny leg: small enough that a padded write file pushes the pack over it.
TINY_LEG = Leg(id="tiny-free", tier="free", harness="opencode", model="tiny-model",
               family="tiny", windows=("128k",), max_request_tokens=3000,
               trains_on_data=False)


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path / "keeper")
    yield world
    world.close()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("WRITE_MARKER = 1\n", encoding="utf-8")
    (root / "src" / "b.py").write_text("READ_MARKER = 2\n", encoding="utf-8")
    return root


def card(**kw):
    base = {"outcome": "add the thing", "write_set": ["src/a.py"], "read_set": ["src/b.py"],
            "check_cmd": "true", "window_class": "128k", "risk_tags": [],
            "estimate_tokens": 40000}
    base.update(kw)
    return base


def put(w, key, **kw):
    w.store.put_card(w.p("router"), key, card(**kw))


def gate(w, repo, key, **kw):
    return service.gate_card(w.store, w.p("router"), key, repo_root=repo, **kw)


def state_of(w, key):
    return w.conn.execute("SELECT state FROM items WHERE key=?", (key,)).fetchone()[0]


def open_questions(w, key):
    return w.conn.execute("SELECT id, text, default_answer FROM questions"
                          " WHERE key=? AND state='open'", (key,)).fetchall()


def test_ready_card_moves_to_ready(w, repo):
    (key,) = seed(w, [("A", [])])
    put(w, key)
    result = gate(w, repo, key)
    assert result.ready and result.acted and result.state == "ready"
    assert result.question_id is None and result.route_error is None
    assert result.route.first_leg.harness == "qodercli"
    assert state_of(w, key) == "ready"
    assert gate(w, repo, key).state == "ready"
    assert open_questions(w, key) == []


def test_missing_check_needs_input_with_one_question(w, repo):
    (key,) = seed(w, [("A", [])])
    put(w, key, check_cmd=None)
    result = gate(w, repo, key)
    assert not result.ready and result.state == "needs_input"
    assert "no_check" in result.verdict.codes()
    assert state_of(w, key) == "needs_input"
    rows = open_questions(w, key)
    assert len(rows) == 1 and rows[0][2] is None
    assert result.question_id == rows[0][0]
    assert "no_check" in rows[0][1] and "not ready" in rows[0][1]
    again = gate(w, repo, key)
    assert again.question_id == result.question_id and again.state == "needs_input"
    assert len(open_questions(w, key)) == 1


def test_fixed_card_goes_ready_again(w, repo):
    (key,) = seed(w, [("A", [])])
    put(w, key, check_cmd=None)
    assert gate(w, repo, key).state == "needs_input"
    put(w, key)
    assert gate(w, repo, key).state == "ready"
    assert state_of(w, key) == "ready"


def test_owned_needs_input_is_left_to_worker(w, repo):
    from keeper_util import seed_exec_owner
    key, = seed(w, [("A", [])])
    put(w, key)
    w.store.set_state(w.p("router"), key, "needs_input")
    seed_exec_owner(w, key, ("wf", "run", 1))
    before = dict(w.store._item(key))
    result = gate(w, repo, key)
    assert not result.acted and result.state == "needs_input"
    assert dict(w.store._item(key)) == before


def test_read_file_missing(w, repo):
    (key,) = seed(w, [("A", [])])
    put(w, key, read_set=["src/nope.py"])
    result = gate(w, repo, key)
    assert result.state == "needs_input"
    assert [f.message for f in result.verdict.findings if f.code == "read_missing"] \
        == ["src/nope.py"]


def test_write_overlap_with_running_card(w, repo):
    a, b = seed(w, [("A", []), ("B", [])])
    put(w, b, write_set=["src/a.py"])
    w.store.set_state(w.p("router"), b, "running")
    put(w, a, write_set=["src/a.py"])
    assert service.running_write_sets(w.store, "demo") == {b: ["src/a.py"]}
    assert service.running_write_sets(w.store, "demo", exclude=b) == {}
    result = gate(w, repo, a)
    assert "write_overlap" in result.verdict.codes() and result.state == "needs_input"
    # the other card stops running: no overlap any more
    w.store.set_state(w.p("router"), b, "parked")
    assert service.running_write_sets(w.store, "demo") == {}
    assert gate(w, repo, a).state == "ready"


def test_running_card_of_other_project_does_not_overlap(w, repo):
    (a,) = seed(w, [("A", [])])
    (o,) = seed(w, [("O", [])], project="other")
    put(w, o, write_set=["src/a.py"])
    w.store.set_state(w.p("router"), o, "running")
    assert service.running_write_sets(w.store, "demo") == {}
    put(w, a)
    assert gate(w, repo, a).state == "ready"


def test_estimate_too_big_for_class(w, repo):
    (key,) = seed(w, [("A", [])])
    put(w, key, window_class="128k", estimate_tokens=100000)
    result = gate(w, repo, key)
    assert "estimate_too_big" in result.verdict.codes()
    assert result.state == "needs_input"


def test_pack_over_first_leg_max_needs_input(w, repo):
    (key,) = seed(w, [("A", [])])
    put(w, key, estimate_tokens=1000)
    (repo / "src" / "a.py").write_text(
        "WRITE_MARKER = 1\n" + "# padding\n" * 3000, encoding="utf-8")
    result = gate(w, repo, key, legs=(TINY_LEG,))
    assert result.state == "needs_input" and not result.ready
    assert result.verdict.codes() == ["leg_request_too_big"]
    assert any(f.code == "leg_request_too_big" and "pack " in f.message
               for f in result.verdict.findings)
    assert state_of(w, key) == "needs_input"
    rows = open_questions(w, key)
    assert len(rows) == 1 and rows[0][2] is None
    assert "leg_request_too_big" in rows[0][1] and "pack" in rows[0][1]
    assert result.question_id == rows[0][0]
    again = gate(w, repo, key, legs=(TINY_LEG,))
    assert again.question_id == result.question_id and again.state == "needs_input"
    assert len(open_questions(w, key)) == 1


def test_pack_too_big_reports_pack_finding(w, repo):
    (key,) = seed(w, [("A", [])])
    pads = [f"src/pad{i}.py" for i in range(4)]
    for name in pads:
        (repo / name).write_text("x" * 90000, encoding="utf-8")
    put(w, key, estimate_tokens=1000, write_set=pads)
    result = gate(w, repo, key, legs=(TINY_LEG,))
    assert result.state == "needs_input"
    assert result.verdict.codes() == ["leg_request_too_big"]
    assert any(f.message.startswith("pack too big: ") for f in result.verdict.findings)
    rows = open_questions(w, key)
    assert len(rows) == 1 and "leg_request_too_big" in rows[0][1]
    assert "pack too big" in rows[0][1]


def test_all_legs_unavailable_gives_no_route(w, repo):
    (key,) = seed(w, [("A", [])])
    put(w, key)
    result = gate(w, repo, key, unavailable=[leg.id for leg in DEFAULT_LEGS])
    assert result.route is None and result.route_error
    assert "no_route" in result.verdict.codes()
    assert not result.ready and result.state == "needs_input"
    assert "no_route" in open_questions(w, key)[0][1]


def test_non_gateable_state_is_left_alone(w, repo):
    (key,) = seed(w, [("A", [])])
    put(w, key, check_cmd=None)
    w.store.set_state(w.p("router"), key, "running")
    result = gate(w, repo, key)
    assert result.acted is False and result.state == "running"
    assert not result.ready and result.question_id is None
    assert state_of(w, key) == "running" and open_questions(w, key) == []


def test_risk_tags_pass_to_question_without_default(w, repo):
    (key,) = seed(w, [("A", [])])
    put(w, key, check_cmd=None, risk_tags=["auth"])
    result = gate(w, repo, key)
    row = w.conn.execute("SELECT risk_tags, default_answer FROM questions WHERE id=?",
                         (result.question_id,)).fetchone()
    assert "auth" in row[0] and row[1] is None


def test_import_batch_writes_cards(w, tmp_path):
    (key,) = seed(w, [("A", [])])
    path = tmp_path / "batch.yaml"
    path.write_text(
        "cards:\n"
        "  demo:A:\n"
        "    outcome: imported outcome\n"
        "    write_set: [src/a.py]\n"
        "    check_cmd: 'true'\n"
        "    risk_tags: []\n"
        "    estimate_tokens: 1000\n", encoding="utf-8")
    assert service.import_batch(w.store, w.p("router"), path) == [key]
    stored = w.store.get_card(w.p("router"), key)
    assert stored["outcome"] == "imported outcome"
    assert stored["write_set"] == ["src/a.py"] and stored["estimate_tokens"] == 1000


def test_import_batch_default_project_for_bare_ids(w, tmp_path):
    (key,) = seed(w, [("A", [])])
    path = tmp_path / "bare.yaml"
    path.write_text("cards:\n  A:\n    outcome: bare id card\n", encoding="utf-8")
    assert service.import_batch(w.store, w.p("router"), path, "demo") == [key]
    assert w.store.get_card(w.p("router"), key)["outcome"] == "bare id card"


def test_pack_includes_write_content_and_hides_heldout(w, repo):
    (key,) = seed(w, [("A", [])])
    put(w, key, heldout_check="HELDOUT_SECRET_CHECK")
    pack = service.pack_for_card(w.store, w.p("router"), key, repo_root=repo)
    assert "WRITE_MARKER" in pack.text and "READ_MARKER" in pack.text
    assert "HELDOUT_SECRET_CHECK" not in pack.text
    assert [f.role for f in pack.files] == ["write", "read"]


def test_pack_needs_a_window_class(w, repo):
    (key,) = seed(w, [("A", [])])
    put(w, key, window_class=None)
    with pytest.raises(ValueError):
        service.pack_for_card(w.store, w.p("router"), key, repo_root=repo)
    pack = service.pack_for_card(w.store, w.p("router"), key, repo_root=repo,
                                 window_class="256k", check_tail="FAILED x")
    assert "FAILED x" in pack.text


def test_route_for_card_prefers_qodercli(w):
    (key,) = seed(w, [("A", [])])
    put(w, key, estimate_tokens=40000, risk_tags=[])
    route = service.route_for_card(w.store, w.p("router"), key)
    assert route.first_leg.harness == "qodercli"
    assert route.window_class == "128k"
