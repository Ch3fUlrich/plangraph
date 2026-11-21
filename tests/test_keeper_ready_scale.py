"""LE-KEEPER-READY-READ-OVER-CLIENT-TIMEOUT: ``ready`` answers inside the client
timeout on a 4000+ item plan, never rebuilds the work index in a request once one
exists, and the read-only fallback finds the daemon's default snapshot."""
import json
import os
import threading
import time
import urllib.request
from types import SimpleNamespace

import pytest

from keeper_util import World, defn
from plangraph.keeper import api, cli_verbs, config, context_sources as src

N_LANES = 4300


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    world = World(tmp_path_factory.mktemp("big"))
    for g in range(N_LANES // 200 + 1):
        ids = [f"L{g}-{i}" for i in range(200)]
        world.store.create_items(world.p("router"), [defn(i) for i in ids]
                                 + [defn(f"G{g}", ids, kind="goal")])
    yield world
    world.close()


@pytest.fixture
def served(big, tmp_path):
    path = tmp_path / "keeper.json"
    path.write_text(json.dumps({"db": str(big.path), "bind": "127.0.0.1:0",
                                "snapshot": str(tmp_path / "plan.snapshot.json")}),
                    encoding="utf-8")
    os.chmod(path, 0o600)
    server = api.make_server(big.store, config.load_config(path), port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def get(base, token, query):
    request = urllib.request.Request(f"{base}/v1/ready?{query}",
                                     headers={"Authorization": f"Bearer {token}"})
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=cli_verbs.TIMEOUT) as reply:
        body = reply.read()
    return time.monotonic() - started, body


def test_plan_fixture_has_four_thousand_items(big):
    assert len(big.store._all_items()) > 4000


@pytest.mark.parametrize("query", ["fmt=json", "fmt=text&n=10", "fmt=json&all=1"])
def test_ready_on_4000_items_answers_inside_the_client_timeout(big, served, query):
    elapsed, body = get(served, big.tokens["router"], query)
    assert elapsed < cli_verbs.TIMEOUT
    assert body


def test_json_ready_builds_the_groups_once(big, served, monkeypatch):
    real, calls = big.store._ready_items, []
    monkeypatch.setattr(big.store, "_ready_items",
                        lambda *a, **kw: calls.append(1) or real(*a, **kw))
    get(served, big.tokens["router"], "fmt=json")
    assert len(calls) == 1


# ------------------------------------------------------------ work index
class FakeGit:
    pass


def _patch_index(monkeypatch, gate):
    """``work_index`` replaced by a counting fake that fills the real cache."""
    built = []

    def work_index(git, project, *, fresh=False):
        built.append(threading.current_thread().name)
        gate.wait(5)
        result = {"stale": False, "origin_stale": False, "built": len(built)}
        git._context_work_index = {("repo", "ref"): (time.monotonic(), result)}
        return result

    monkeypatch.setattr(src, "work_index", work_index)
    monkeypatch.setattr(src, "_repo_ref", lambda git, project: ("repo", "ref"))
    monkeypatch.setattr(src, "_REFRESHING", set())
    return built


def test_cold_read_builds_in_the_request_expired_read_serves_the_old_index(monkeypatch):
    gate, git = threading.Event(), FakeGit()
    gate.set()
    built = _patch_index(monkeypatch, gate)
    assert src.work_index_cached(git, "demo")["built"] == 1
    assert len(built) == 1 and built[0] == threading.current_thread().name
    assert src.work_index_cached(git, "demo")["built"] == 1        # fresh: no rebuild
    assert len(built) == 1

    gate.clear()
    stamp, result = git._context_work_index[("repo", "ref")]
    git._context_work_index[("repo", "ref")] = (stamp - 3600, result)
    started = time.monotonic()
    answers = [src.work_index_cached(git, "demo")["built"] for _ in range(3)]
    assert time.monotonic() - started < 2           # not blocked on the rebuild
    assert answers == [1, 1, 1]                     # the last built index
    deadline = time.monotonic() + 5
    while len(built) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(built) == 2 and built[1] == "work-index-refresh"   # ONE refresh
    gate.set()
    deadline = time.monotonic() + 5
    while src._REFRESHING and time.monotonic() < deadline:
        time.sleep(0.01)
    assert src.work_index_cached(git, "demo")["built"] == 2        # new index served


# ------------------------------------------------------------ the client
@pytest.mark.parametrize("verb", ["ready", "blocked", "waves"])
def test_slow_reads_use_the_slow_client_timeout(monkeypatch, verb):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["timeout"] = timeout
        raise OSError("stop")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("PG_URL", "http://127.0.0.1:1")
    session = cli_verbs.Session()
    session._token = "t"
    cli_verbs.read(session, verb, {}, lambda r: "")
    assert seen["timeout"] == cli_verbs.SLOW_TIMEOUT
    cli_verbs.read(session, "alerts", {}, lambda r: "")
    assert seen["timeout"] == cli_verbs.TIMEOUT


def test_no_config_snapshot_path_is_the_daemon_default_not_the_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PG_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    path = cli_verbs.Session().snapshot_path()
    assert path == tmp_path / "home" / ".local/share/plangraph/plan.snapshot.json"
    assert path.is_absolute()


def test_fallback_without_config_reads_the_default_snapshot(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    snap = home / ".local/share/plangraph/plan.snapshot.json"
    snap.parent.mkdir(parents=True)
    item = {"key": "demo:A", "project": "demo", "id": "A", "title": "alpha",
            "state": "open", "kind": "lane", "depends_on": [], "priority": 5,
            "operator": False, "track": "T0", "owner": "x", "estimate_h": 1}
    snap.write_text(json.dumps({"items": [item], "head_seq": 1, "generated": "now"}),
                    encoding="utf-8")
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PG_CONFIG", raising=False)
    monkeypatch.setenv("PG_URL", "http://127.0.0.1:1")      # nothing listens
    monkeypatch.setenv("PG_TOKEN_FILE", str(tmp_path / "t"))
    (tmp_path / "t").write_text("x", encoding="utf-8")
    from plangraph.keeper import cli
    assert cli.main(["ready"]) == 0
    out = capsys.readouterr()
    assert "READ-ONLY fallback" in out.err and "demo:A" in out.out


def test_fallback_with_no_snapshot_anywhere_is_a_refusal_not_a_crash(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PG_CONFIG", raising=False)
    monkeypatch.setenv("PG_URL", "http://127.0.0.1:1")
    (tmp_path / "t").write_text("x", encoding="utf-8")
    monkeypatch.setenv("PG_TOKEN_FILE", str(tmp_path / "t"))
    from plangraph.keeper import cli
    assert cli.main(["ready"]) == cli_verbs.EXIT_UNREACHABLE
    assert "no usable local snapshot" in capsys.readouterr().err


def test_an_expired_index_is_served_with_its_age_while_the_refresh_runs(big, served, monkeypatch):
    """Through the 4000-item endpoint: the index expired, the refresh is slow; the
    read answers inside the client timeout and says the index is old."""
    gate = threading.Event()
    git = FakeGit()
    result = {"stale": False, "origin_stale": False, "commits": {}, "prs": {},
              "branches": [], "order": {}, "verified_at": time.time() - 3600}
    git._context_work_index = {("repo", "ref"): (time.monotonic() - 3600, result)}
    built = []

    def slow_index(g, project, *, fresh=False):
        built.append(project)
        gate.wait(30)
        return result

    monkeypatch.setattr(src, "work_index", slow_index)
    monkeypatch.setattr(src, "_repo_ref", lambda g, project: ("repo", "ref"))
    monkeypatch.setattr(src, "_REFRESHING", set())
    monkeypatch.setattr(big.store, "_adapter", lambda name: git if name == "git" else None)
    try:
        elapsed, body = get(served, big.tokens["router"], "fmt=json&n=3")
        assert elapsed < cli_verbs.TIMEOUT
        data = json.loads(body)
        assert len(data["items"]) == 3                       # still answers, rows ready
        note, = [w for w in data["warnings"] if w["status"] == "refreshing"]
        assert note["project"] == "demo" and note["age_seconds"] >= 3600
        text = get(served, big.tokens["router"], "fmt=text&n=3")[1].decode()
        assert "work index stale" in text and "refresh running" in text
    finally:
        gate.set()
    deadline = time.monotonic() + 10
    while src._REFRESHING and time.monotonic() < deadline:
        time.sleep(0.01)
    assert built == ["demo"]                                  # one refresh only
