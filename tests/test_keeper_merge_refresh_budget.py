"""The keeper merge-index refresh: a scan budget, a terminal failed state and resume."""
import json
import logging
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock, current_thread, local
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

from door_util import Doors
from plangraph.keeper import api_routes, context_sources, store_context, store_review, timing
from test_keeper_merge_close_auto import Repo, env, git, set_states  # noqa: F401  (includes the shared fixture)


_REFRESH_PATH = "/v1/close_merged_auto"
_TERMINAL = "merge_index_refresh "


def _await_scan(doors, path):
    import threading
    for thread in threading.enumerate():
        if thread.name == "keeper-merge-index":
            thread.join(10)
    return doors.http("system", "POST", path, {"refresh": "status"})


@pytest.fixture
def refresh_door(env, tmp_path):
    doors = Doors(env, tmp_path)
    try:
        yield doors
    finally:
        doors.close()


@pytest.fixture
def scan_clock(monkeypatch):
    now_ms = 0

    def advance(ms):
        nonlocal now_ms
        now_ms += ms

    def seconds():
        return now_ms / 1000

    monkeypatch.setattr(api_routes, "monotonic", seconds)
    return SimpleNamespace(advance=advance, seconds=seconds)


def _delay(clock, ms, fn):
    def delayed(*args, **kwargs):
        clock.advance(ms)
        return fn(*args, **kwargs)
    return delayed


def _records(caplog, capsys=None):
    records = [message for message in caplog.messages if message.startswith(_TERMINAL)]
    if capsys is not None:
        records += [line.removeprefix("keeper: ") for line in capsys.readouterr().err.splitlines()
                    if line.startswith("keeper: " + _TERMINAL)]
    return records


def _fields(record):
    return dict(field.split("=", 1) for field in record.split() if "=" in field)


def test_deploy_reads_overlap_landing_work_with_a_bounded_pool(
        env, refresh_door, monkeypatch, caplog):
    caplog.set_level(logging.INFO, "plangraph.keeper")
    landings = {}
    for pr, key in enumerate("ABCPSNH", 81):
        head = env.repo.land(key.lower(), f"Plan-Item: demo:{key}", pr)
        sha = git(env.repo.root, "rev-parse", "HEAD")
        landings[sha] = {"head": {"sha": head}, "number": pr}
        if key == "C":
            named = sha
    env.conn.execute("UPDATE items SET description=? WHERE key='demo:C'", (f"Merge-Repo: demo {named}",))
    git_adapter = env.store.ctx["git"]
    read_diff = git_adapter.deploy_changes
    lock, overlap, first_deploy = Lock(), Event(), Event()
    workers, named_readers, budgets = set(), set(), []
    active = peak = 0
    concurrent = False

    def deploy(repo, sha, target, key):
        nonlocal active, peak
        thread = current_thread().name
        if key == "demo:C":
            named_readers.add(thread)
        elif thread != "keeper-merge-index":
            with lock:
                workers.add(thread)
                budgets.append(timing.io_timeout(1000))
                active += 1
                peak = max(peak, active)
                first_deploy.set()
            try:
                sleep(0.08)
            finally:
                with lock:
                    active -= 1
        else:
            sleep(0.08)
        if key == "demo:N":
            raise RuntimeError("unreadable diff")
        return read_diff(repo, sha, target, key)

    def merged_pr(_project, sha, _ref):
        if concurrent and sha == named:
            assert first_deploy.wait(5)
        with lock:
            if active:
                overlap.set()
        return landings[sha]

    monkeypatch.setattr(git_adapter, "deploy_changes", deploy)
    env.store.ctx["ci"] = SimpleNamespace(
        mode=lambda project: project, merged_pr_for=merged_pr,
        check_runs=lambda _project, _sha: [{"name": "ci", "conclusion": "success"}])
    server = refresh_door.server

    def scan():
        assert refresh_door.http("system", "POST", _REFRESH_PATH, {"refresh": "start"}) == (
            200, {"scan": "running"})
        assert _await_scan(refresh_door, _REFRESH_PATH) == (200, {"scan": "ready"})
        return server.merge_refresh._bundle

    with monkeypatch.context() as serial:
        serial.setattr(store_context, "_MERGE_REFRESH_WORKERS", 0)
        expected = scan()
    concurrent = True
    server.merge_refresh = api_routes.MergeRefresh(server)
    actual = scan()

    assert actual == expected
    assert actual["_deploy_warm"]["demo", "demo:N"]["changes"] is None
    target = actual["_warmed"]["demo"]["target"]
    assert actual["_named_landings"]["demo:C", target] == named
    assert ("demo:N", target) in actual["_named_landings"]
    assert 1 <= peak <= len(workers) <= 4
    assert len(budgets) == 6 and all(0 < left <= 180 for left in budgets)
    assert named_readers == {"keeper-merge-index"}
    assert overlap.is_set()
    record = _fields(_records(caplog)[-1])
    assert record["landing_warm_attempts"] == record["open_lanes_attempts"] == "7"
    assert record["deploy_snapshot_attempts"] == "7"
    assert record["merged_pr_for_attempts"] == record["check_runs_attempts"] == "7"
    assert int(record["deploy_snapshot_ms"]) >= 6 * 80


def test_deploy_worker_keeps_an_explicit_repository_hint(env, refresh_door, tmp_path):
    checkout = tmp_path / "other"
    checkout.mkdir()
    Repo(checkout).land("other-work", "Plan-Item: demo:A", 81)
    env.store.ctx["git"]._repos["other"] = str(checkout)
    env.conn.execute("UPDATE items SET description=? WHERE key='demo:A'", ("Merge-Repo: demo",))

    assert refresh_door.http("system", "POST", _REFRESH_PATH, {"refresh": "start"})[0] == 200
    assert _await_scan(refresh_door, _REFRESH_PATH) == (200, {"scan": "ready"})
    bundle = refresh_door.server.merge_refresh._bundle
    assert bundle["_deploy_warm"]["other", "demo:A"]["changes"] is None
    assert ("demo:A", bundle["_warmed"]["other"]["target"]) not in bundle["_named_landings"]


def _hosted_sweep(env, tmp_path, monkeypatch, lane_calls):
    root, hosted = env.repo.root, "example/fixture"
    tips = {name: env.repo.land(name, "unmerged work", 0, merge=False) for name in ("lane/ab", "lane/ac")}
    remote = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(root), str(remote))
    git(root, "remote", "add", "origin", str(remote))
    expected = {}
    for number, (branch, tip) in enumerate(tips.items(), 101):
        git(remote, "update-ref", f"refs/pull/{number}/head", tip)
        expected[str(root), hosted, branch, tip] = number
    adapter = env.store.ctx["git"]
    real_git, real_run = adapter._run, subprocess.run

    def fake_origin(repo, args, **kwargs):
        if args == ["remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, f"https://github.com/{hosted}.git\n", "")
        return real_git(repo, args, **kwargs)

    def fake_forge(args, *rest, **kwargs):
        if args[:3] != ["gh", "pr", "list"]:
            return real_run(args, *rest, **kwargs)
        if "--head" not in args:
            assert args[args.index("--state") + 1] == "open"
            return subprocess.CompletedProcess(args, 0, "[]", "")
        branch = args[args.index("--head") + 1]
        key = str(kwargs["cwd"]), args[args.index("--repo") + 1], branch, tips[branch]
        with lock:
            lane_calls[key] += 1
        response = [{"number": expected[key], "state": "CLOSED", "headRefOid": tips[branch],
                     "isCrossRepository": False}]
        return subprocess.CompletedProcess(args, 0, json.dumps(response), "")

    lock = Lock()
    monkeypatch.setattr(context_sources, "time", SimpleNamespace(monotonic=monotonic, time=env.store.now))
    monkeypatch.setattr(adapter, "_run", fake_origin)
    monkeypatch.setattr(subprocess, "run", fake_forge)
    return expected


@pytest.mark.parametrize("workers", [1, 2, 4])
@pytest.mark.parametrize("hosted", [False, True], ids=["local", "hosted"])
def test_refresh_worker_sweep_reuses_only_generation_forge_reads(
        env, refresh_door, tmp_path, monkeypatch, caplog, workers, hosted):
    caplog.set_level(logging.INFO, "plangraph.keeper")
    landings = set()
    for number, keys in enumerate(("AB", "C", "H", "N", "P", "S"), 81):
        env.repo.land(keys.lower(), "\n".join(f"Plan-Item: demo:{key}" for key in keys), number)
        landings.add(git(env.repo.root, "rev-parse", "HEAD"))
    head = "d" * 40
    calls, active, threads = Counter(), Counter(), set()
    lane_calls = Counter()
    lane_records = _hosted_sweep(env, tmp_path, monkeypatch, lane_calls) if hosted else {}
    lock = Lock()
    peak, overlap, arrivals, barrier = 0, False, 0, None

    def slow(stage, fn):
        nonlocal peak, overlap, arrivals
        with lock:
            active[stage] += 1
            threads.add(current_thread().name)
            peak = max(peak, sum(active.values()))
            overlap |= sum(count > 0 for count in active.values()) > 1
            gate = barrier if barrier is not None and arrivals < barrier.parties else None
            arrivals += 1
        try:
            if gate is not None:
                gate.wait()
            if stage != "landing_warm":
                sleep(0.025)
            return fn()
        finally:
            with lock:
                active[stage] -= 1

    def forge(stage, key, fn):
        with lock:
            calls[stage, key] += 1
        sleep(0.025)
        return fn()

    def checks(_mode, sha):
        def unreadable():
            raise RuntimeError("stable unreadable check-runs")
        return forge("check_runs", sha, unreadable)

    git_adapter = env.store.ctx["git"]
    real_deploy = git_adapter.deploy_changes
    real_lanes = context_sources.open_lanes

    def lanes(git_, project, key, **opts):
        def read():
            result = real_lanes(git_, project, key, **opts)
            if not hosted:
                opts["cache"][project, "example/fixture", key, opts["head"]] = 100
            return result
        return slow("open_lanes", read)

    read_landings = store_review._read_landings
    monkeypatch.setattr(store_review, "_read_landings", lambda *args: slow(
        "landing_warm", lambda: read_landings(*args)))
    monkeypatch.setattr(git_adapter, "deploy_changes", lambda repo, sha, ref, key: slow(
        "deploy_snapshot", lambda: real_deploy(repo, sha, ref, key)))
    monkeypatch.setattr(context_sources, "open_lanes", lanes)
    env.store.ctx["ci"] = SimpleNamespace(
        mode=lambda project: project, check_runs=checks, merged_pr_for=lambda project, sha, ref: forge(
            "merged_pr_for", (project, sha, ref), lambda: {"head": {"sha": head}}))
    refresh = refresh_door.server.merge_refresh

    def scan(size):
        nonlocal peak, overlap, arrivals, barrier
        calls.clear()
        lane_calls.clear()
        threads.clear()
        peak, overlap, arrivals = 0, False, 0
        barrier = Barrier(size, timeout=5) if size > 1 else None
        monkeypatch.setattr(store_context, "_MERGE_REFRESH_WORKERS", size)
        refresh.expires_at = 0
        assert refresh_door.http("system", "POST", _REFRESH_PATH, {"refresh": "start"})[0] == 200
        assert _await_scan(refresh_door, _REFRESH_PATH) == (200, {"scan": "ready"})
        assert barrier is None or not barrier.broken
        return refresh._bundle, _fields(_records(caplog)[-1])

    with monkeypatch.context() as serial:
        serial.setattr(timing, "merge_lookup", lambda _key, fetch: fetch(), raising=False)
        expected, baseline = scan(0)
    assert baseline["merged_pr_for_attempts"] == baseline["check_runs_attempts"] == "7"
    serial_bundle, serial_record = scan(0)
    serial_lane_calls = lane_calls.copy()
    actual, fields = scan(workers)
    assert actual == serial_bundle == expected
    if hosted:
        assert actual["_lanes"] == lane_records
        assert lane_calls == serial_lane_calls == Counter(dict.fromkeys(lane_records, 1))
    else:
        assert len(actual["_lanes"]) == 7 and set(actual["_lanes"].values()) == {100}
    assert all(value[0]["checks"]["detail"] == "check_runs_unavailable"
               for value in actual["_landings"].values())
    assert fields["generation"] == "3"
    assert {key[1][1] for key, count in calls.items() if key[0] == "merged_pr_for" and count == 1} == landings
    assert calls["check_runs", head] == 1
    assert fields["merged_pr_for_attempts"] == serial_record["merged_pr_for_attempts"] == "6"
    assert fields["check_runs_attempts"] == serial_record["check_runs_attempts"] == "1"
    assert fields["deploy_snapshot_attempts"] == fields["open_lanes_attempts"] == fields["landing_warm_attempts"] == "7"
    assert peak == len(threads) == workers
    assert overlap == (workers > 1)
    stages = {"landing_warm", "open_lanes", "deploy_snapshot"}
    assert all(int(fields[f"{stage}_ms"]) > 0 for stage in stages)
    io_ms = sum(int(fields[f"{stage}_ms"]) for stage in stages)
    print(f"refresh sweep workers={workers} wall_ms={fields['elapsed_ms']} io_ms={io_ms} peak={peak} "
          f"merged_pr_for={fields['merged_pr_for_attempts']} check_runs={fields['check_runs_attempts']}")


def test_refresh_timing_sums_concurrent_work_and_subtracts_only_covered_wall_time(caplog):
    caplog.set_level(logging.INFO, "plangraph.keeper")
    clock = local()
    counters = timing.MergeRefreshTiming(0, lambda: clock.seconds)
    all_started = Barrier(3, timeout=5)

    def deploy(start, end, fails=False):
        clock.seconds = start
        with counters.stage("deploy_snapshot"):
            all_started.wait()
            clock.seconds = end
            if fails:
                raise RuntimeError("unreadable diff")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(deploy, 0.010, 0.050)
        second = pool.submit(deploy, 0.020, 0.080, True)
        clock.seconds = 0.030
        with counters.stage("landing_warm"):
            all_started.wait()
            clock.seconds = 0.045
        first.result()
        with pytest.raises(RuntimeError, match="unreadable diff"):
            second.result()

    clock.seconds = 0.100
    counters.emit(1, "ready", None)
    record = _fields(_records(caplog)[-1])
    assert record["deploy_snapshot_attempts"] == "2"
    assert record["deploy_snapshot_ms"] == "100"
    assert record["landing_warm_ms"] == "15"
    assert record["elapsed_ms"] == "100" and record["residual_ms"] == "30"


@pytest.mark.parametrize("info_enabled", [True, False])
def test_refresh_reports_stage_counters_on_ready_generation(
        env, refresh_door, scan_clock, monkeypatch, caplog, capsys, info_enabled):
    caplog.set_level(logging.INFO if info_enabled else logging.WARNING, "plangraph.keeper")
    monkeypatch.setattr(store_context, "_MERGE_REFRESH_WORKERS", 0)
    set_states(env)
    env.repo.land("c", "Plan-Item: demo:C", 84)
    refresh = refresh_door.server.merge_refresh
    adapter = env.store.ctx["git"]
    store = type(env.store)
    for obj, name, ms in [
        (refresh, "_read_candidates", 11), (adapter, "scan_projects", 2.6),
        (store, "_snapshot_landing_decisions", 13), (context_sources, "work_index", 31),
        (context_sources, "indexed_work", 0.6), (store, "_warm_landings", 17.6),
        (context_sources, "open_lanes", 19), (store, "_snapshot_deploy", 23),
    ]:
        monkeypatch.setattr(obj, name, _delay(scan_clock, ms, getattr(obj, name)))
    monkeypatch.setattr(store, "_refresh_merged_default_ref",
                        staticmethod(_delay(scan_clock, 22, store._refresh_merged_default_ref)))

    post = lambda **body: refresh_door.http("system", "POST", _REFRESH_PATH, body)
    assert post(refresh="start") == (200, {"scan": "running"})
    assert _await_scan(refresh_door, _REFRESH_PATH) == (200, {"scan": "ready"})
    messages = _records(caplog)
    assert len(messages) == int(info_enabled)
    record, = _records(caplog, capsys)
    assert _fields(record) == {
        "repos": "1", "failed": "0", "seconds": "0.1", "generation": "1", "outcome": "ready",
        "elapsed_ms": "143", "residual_ms": "3", "landing_cache_hits": "0",
        "candidate_snapshot_ms": "11", "candidate_snapshot_attempts": "1",
        "decision_snapshot_ms": "13", "decision_snapshot_attempts": "1",
        "default_ref_ms": "22", "default_ref_attempts": "1",
        "work_index_ms": "31", "work_index_attempts": "1",
        "delivery_lookup_ms": "4", "delivery_lookup_attempts": "7",
        "landing_warm_ms": "18", "landing_warm_attempts": "1",
        "open_lanes_ms": "19", "open_lanes_attempts": "1",
        "deploy_snapshot_ms": "23", "deploy_snapshot_attempts": "1",
        "publish_wait_ms": "0", "publish_wait_attempts": "1",
        "merged_pr_for_ms": "0", "merged_pr_for_attempts": "0",
        "check_runs_ms": "0", "check_runs_attempts": "0",
    }
    bundle = refresh._bundle
    assert bundle and "demo" in bundle["_warmed"]
    assert env.store.__dict__.get("_warmed") is not bundle["_warmed"]
    status, result = post(require_seats=False)
    assert status == 200 and [row["key"] for row in result["closed"]] == ["demo:C"]
    assert refresh._bundle is None and env.store._warmed is bundle["_warmed"]
    assert post(refresh="status") == post(refresh="resume") == (200, {"scan": "ready"})
    assert post(refresh="projects") == (200, {"projects": ["demo"]})
    assert _records(caplog, capsys) == messages


def test_refresh_separates_landing_cache_pr_and_check_calls(
        env, refresh_door, scan_clock, monkeypatch, caplog):
    caplog.set_level(logging.INFO, "plangraph.keeper")
    monkeypatch.setattr(store_context, "_MERGE_REFRESH_WORKERS", 0)
    set_states(env)
    landings = {}
    for key, pr in [("C", 84), ("S", 85), ("N", 86)]:
        head = env.repo.land(key.lower(), f"Plan-Item: demo:{key}", pr)
        landings[pr] = git(env.repo.root, "rev-parse", "HEAD"), head
    hits = {"checks": {"verified": True, "detail": "CI green"}}
    env.store._landings = {("demo", landings[84][0]): (hits, 0)}
    merged = {sha: {"head": {"sha": head}, "number": pr} for pr, (sha, head) in landings.items()}
    prs, checks = [], []

    def merged_pr(project, sha, _target):
        prs.append((project, sha))
        return merged[sha]

    def check_runs(mode, head):
        checks.append((mode, head))
        if head == landings[86][1]:
            raise RuntimeError("unavailable forge output")
        return [{"name": "ci", "conclusion": "success"}]

    adapter = env.store.ctx["git"]
    monkeypatch.setattr(adapter, "scan_projects", _delay(scan_clock, 5.3, adapter.scan_projects))
    env.store.ctx["ci"] = SimpleNamespace(
        mode=lambda project: project, merged_pr_for=_delay(scan_clock, 7.6, merged_pr),
        check_runs=_delay(scan_clock, 11.6, check_runs))

    assert refresh_door.http("system", "POST", _REFRESH_PATH, {"refresh": "start"})[0] == 200
    assert _await_scan(refresh_door, _REFRESH_PATH) == (200, {"scan": "ready"})
    record, = _records(caplog)
    fields = _fields(record)
    assert {key: fields[key] for key in (
        "landing_cache_hits", "landing_warm_attempts", "landing_warm_ms", "merged_pr_for_attempts",
        "merged_pr_for_ms", "check_runs_attempts", "check_runs_ms", "elapsed_ms", "residual_ms",
    )} == {
        "landing_cache_hits": "1", "landing_warm_attempts": "3", "landing_warm_ms": "38",
        "merged_pr_for_attempts": "2", "merged_pr_for_ms": "15", "check_runs_attempts": "2",
        "check_runs_ms": "23", "elapsed_ms": "44", "residual_ms": "5",
    }
    assert sorted(prs) == sorted(("demo", landings[pr][0]) for pr in (85, 86))
    assert sorted(checks) == sorted(("demo", landings[pr][1]) for pr in (85, 86))
    refresh = refresh_door.server.merge_refresh
    unavailable, _ = refresh._bundle["_landings"]["demo", landings[86][0]]
    assert unavailable["checks"]["detail"] == "check_runs_unavailable"
    assert unavailable["checks"]["verified"] is False
    assert "unavailable forge output" not in record
    refresh.consume()
    assert env.store._landing_for("demo", landings[84][0]) == hits
    assert env.store._landing_for("demo", landings[86][0]) == unavailable
    assert prs[2:] == [("demo", landings[86][0])] and checks[2:] == [("demo", landings[86][1])]
    assert _records(caplog) == [record]


def test_superseded_refresh_reports_without_publishing(env, refresh_door, monkeypatch, caplog):
    caplog.set_level(logging.INFO, "plangraph.keeper")
    refresh = refresh_door.server.merge_refresh
    store = type(env.store)
    real_warm = store.warm_merge_scan
    newer_bundle = {"_warmed": {}}

    def superseded(scan, candidates):
        real_warm(scan, candidates)
        with refresh._lock:
            refresh._generation += 1
            refresh._bundle = newer_bundle
            refresh.state = "ready"

    monkeypatch.setattr(store, "warm_merge_scan", superseded)
    assert refresh_door.http("system", "POST", _REFRESH_PATH, {"refresh": "start"})[0] == 200
    assert _await_scan(refresh_door, _REFRESH_PATH) == (200, {"scan": "ready"})
    assert refresh._bundle is newer_bundle
    record, = _records(caplog)
    fields = _fields(record)
    assert fields["generation"] == "1" and fields["outcome"] == "superseded"


@pytest.mark.parametrize("failure", ["budget", "scan_error", "publish_wait"])
def test_refresh_reports_terminal_failure_without_partial_publication(
        env, refresh_door, scan_clock, monkeypatch, caplog, capsys, failure):
    log = logging.getLogger("plangraph.keeper")
    caplog.set_level(logging.WARNING, log.name)
    monkeypatch.setattr(timing, "time", SimpleNamespace(monotonic=scan_clock.seconds))
    set_states(env)
    server = refresh_door.server
    refresh, store = server.merge_refresh, type(env.store)
    lock = refresh._lock
    live = env.store.__dict__.setdefault("_landings", {})
    private_key = "demo", "a" * 40
    before = live.copy()
    lock_states = []
    handle = log.handle

    def observe(record):
        if record.getMessage().startswith(_TERMINAL):
            lock_states.append((lock.locked(), server.lock._is_owned()))
        handle(record)

    monkeypatch.setattr(log, "handle", observe)
    post = lambda **body: refresh_door.http("system", "POST", _REFRESH_PATH, body)
    warming, proceed, waiting = Event(), Event(), Event()
    real_decisions = store._snapshot_landing_decisions

    def decisions(scan, candidates):
        real_decisions(scan, candidates)
        scan._landings[private_key] = (None, 0)
        scan_clock.advance(9)
        if failure == "scan_error":
            raise RuntimeError("private scan output")

    def expires(_git, _project):
        scan_clock.advance(180_000)
        timing.check_io_deadline("test default ref")

    real_warm = store.warm_merge_scan

    def warm(scan, candidates):
        real_warm(scan, candidates)
        if failure == "publish_wait":
            warming.set()
            assert proceed.wait(5)

    # Supply a fresh context manager for each acquisition, including the HTTP controls.
    class ObservedLock:
        def __enter__(self):
            if warming.is_set():
                waiting.set()
            return lock.__enter__()

        def __exit__(self, *exc):
            return lock.__exit__(*exc)

    with monkeypatch.context() as fault:
        fault.setattr(store, "_snapshot_landing_decisions", decisions)
        fault.setattr(store, "warm_merge_scan", warm)
        if failure == "budget":
            fault.setattr(store, "_refresh_merged_default_ref", staticmethod(expires))
        if failure == "publish_wait":
            fault.setattr(refresh, "_lock", ObservedLock())
        assert post(refresh="start") == (200, {"scan": "running"})
        if failure == "publish_wait":
            try:
                assert warming.wait(5)
                with lock:
                    proceed.set()
                    assert waiting.wait(5)
                    scan_clock.advance(180_000)
            finally:
                proceed.set()
        assert _await_scan(refresh_door, _REFRESH_PATH) == (200, {"scan": "failed"})

    record, = _records(caplog)
    outcome = "scan_error" if failure == "scan_error" else "budget_exceeded"
    assert record.startswith(f"merge_index_refresh failed {outcome} seconds=")
    fields = _fields(record)
    assert fields["generation"] == "1" and fields["outcome"] == outcome
    assert fields["elapsed_ms"] == ("9" if failure == "scan_error" else "180009")
    assert fields["decision_snapshot_ms"] == "9" and fields["decision_snapshot_attempts"] == "1"
    assert fields["default_ref_ms"] == ("180000" if failure == "budget" else "0")
    assert fields["default_ref_attempts"] == ("0" if failure == "scan_error" else "1")
    assert fields["publish_wait_ms"] == ("180000" if failure == "publish_wait" else "0")
    assert fields["publish_wait_attempts"] == "1"
    if failure != "scan_error":
        assert (fields["repos"], fields["failed"]) == (("0", "1") if failure == "budget" else ("1", "0"))
    assert "private scan output" not in record
    assert lock_states == [(False, False)]
    assert refresh._bundle is None and env.store._landings is live and live == before
    status, refused = post(apply=False)
    assert status == 409 and refused["error"] == "merge_index_failed"

    assert post(refresh="resume") == (200, {"scan": "running"})
    assert _await_scan(refresh_door, _REFRESH_PATH) == (200, {"scan": "ready"})
    failure_record, ready_record = _records(caplog, capsys)
    assert failure_record == record
    ready = _fields(ready_record)
    assert ready["generation"] == "2" and ready["outcome"] == "ready"
    assert ready["decision_snapshot_ms"] == ready["publish_wait_ms"] == ready["elapsed_ms"] == "0"
    assert ready["decision_snapshot_attempts"] == ready["publish_wait_attempts"] == "1"


def test_refresh_deadline_refuses_close_and_allows_restart(env, tmp_path, monkeypatch):
    from door_util import Doors
    from plangraph.keeper import api_routes

    set_states(env)
    env.repo.land("c", "Plan-Item: demo:C", 84)
    doors = Doors(env, tmp_path)
    path = "/v1/close_merged_auto"
    try:
        monkeypatch.setattr(api_routes.MergeRefresh, "SCAN_BUDGET_SECONDS", 0)
        assert doors.http("system", "POST", path, {"refresh": "start"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "failed"})
        refused = (409, {"error": "merge_index_failed", "msg": "merge index refresh failed; start a refresh"})
        assert doors.http("system", "POST", path, {"require_seats": False}) == refused
        assert doors.http("system", "POST", path, {"apply": False}) == refused
        status, body = doors.http("system", "POST", path, {"repos_only": True})
        assert status == 200 and "repos" in body
        assert env.store.show(env.p("router"), "demo:C")["state"] == "in_review"
        monkeypatch.setattr(api_routes.MergeRefresh, "SCAN_BUDGET_SECONDS", 180)
        assert doors.http("system", "POST", path, {"refresh": "start"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "ready"})
        status, body = doors.http("system", "POST", path, {"require_seats": False})
        assert status == 200 and [c["key"] for c in body["closed"]] == ["demo:C"]
    finally:
        doors.close()


def test_resume_reuses_a_fresh_scan_that_another_caller_consumed(env, tmp_path, monkeypatch):
    from door_util import Doors
    from plangraph.keeper import api_routes

    set_states(env)
    env.repo.land("c", "Plan-Item: demo:C", 84)
    tick = [100.0]
    monkeypatch.setattr(api_routes, "monotonic", lambda: tick[0], raising=False)
    doors = Doors(env, tmp_path)
    path = "/v1/close_merged_auto"
    try:
        assert doors.http("system", "POST", path, {"refresh": "start"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "ready"})
        assert doors.http("system", "POST", path, {"apply": False})[0] == 200      # a preview consumes it
        assert doors.http("system", "POST", path, {"refresh": "resume"}) == (200, {"scan": "ready"})
        assert doors.http("system", "POST", path, {"refresh": "start"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "ready"})   # ordinary start still replaces it
        tick[0] += 300
        assert doors.http("system", "POST", path, {"refresh": "resume"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "ready"})
        assert doors.http("system", "POST", path, {"refresh": "bogus"})[0] == 400
    finally:
        doors.close()


def _hold_lock_past_budget(lock, seconds):
    """Take the publication mutex on another thread until the scan expires."""
    import threading
    import time

    held = threading.Event()

    def hold():
        with lock:
            held.set()
            time.sleep(seconds)

    threading.Thread(target=hold, daemon=True).start()
    assert held.wait(5)


def test_a_scan_that_waits_for_the_lock_past_its_budget_is_not_published(env, tmp_path, monkeypatch):
    from door_util import Doors
    from plangraph.keeper import api_routes

    set_states(env)
    doors = Doors(env, tmp_path)
    path = "/v1/close_merged_auto"
    try:
        monkeypatch.setattr(api_routes.MergeRefresh, "SCAN_BUDGET_SECONDS", 0.4)
        monkeypatch.setattr(type(env.store), "warm_merge_scan",
                            lambda store, candidates: _hold_lock_past_budget(doors.server.merge_refresh._lock, 0.8))
        assert doors.http("system", "POST", path, {"refresh": "start"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "failed"})
        assert doors.http("system", "POST", path, {})[1]["error"] == "merge_index_failed"
    finally:
        doors.close()


def test_a_failed_scan_leaves_the_live_landing_cache_alone(env, tmp_path, monkeypatch):
    from door_util import Doors
    from plangraph.keeper import api_routes

    set_states(env)
    doors = Doors(env, tmp_path)
    path = "/v1/close_merged_auto"
    try:
        live = doors.server.store.__dict__.setdefault("_landings", {})
        named = doors.server.store._named_landings
        landing = ("demo:C", "a" * 40)

        real = type(env.store).warm_merge_scan

        def warm(store, candidates):
            real(store, candidates)
            store._landings[("demo", "a" * 40)] = ({"pr": 1}, 0)
            store._named_landings[landing] = "b" * 40

        monkeypatch.setattr(type(env.store), "warm_merge_scan", warm)
        monkeypatch.setattr(api_routes.MergeRefresh, "SCAN_BUDGET_SECONDS", 0)
        assert doors.http("system", "POST", path, {"refresh": "start"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "failed"})
        assert ("demo", "a" * 40) not in live
        assert landing not in named
        monkeypatch.setattr(api_routes.MergeRefresh, "SCAN_BUDGET_SECONDS", 180)
        assert doors.http("system", "POST", path, {"refresh": "start"}) == (200, {"scan": "running"})
        assert _await_scan(doors, path) == (200, {"scan": "ready"})
        assert ("demo", "a" * 40) not in doors.server.store.__dict__["_landings"]
        assert landing not in doors.server.store._named_landings
        assert doors.http("system", "POST", path, {"apply": False})[0] == 200
        assert ("demo", "a" * 40) in doors.server.store.__dict__["_landings"]
        assert doors.server.store._named_landings[landing] == "b" * 40
    finally:
        doors.close()


def test_refresh_candidate_read_and_warm_leave_request_lock_free(env, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from threading import Event
    from door_util import Doors

    doors = Doors(env, tmp_path)
    server, path = doors.server, "/v1/close_merged_auto"
    entered, permit, closed = Event(), Event(), Event()
    warming, warmed = Event(), Event()
    read, warm = server.read_store, type(env.store).warm_merge_scan

    @contextmanager
    def read_view(**kwargs):
        with read(**kwargs) as view:
            entered.set()
            assert permit.wait(3)
            yield view
        closed.set()

    def scan(store, candidates):
        assert closed.is_set() and store.conn is None
        warming.set()
        assert warmed.wait(3)
        return warm(store, candidates)

    monkeypatch.setattr(server, "read_store", read_view)
    monkeypatch.setattr(type(env.store), "warm_merge_scan", scan)
    try:
        assert doors.http("system", "POST", path, {"refresh": "start"})[0] == 200
        assert entered.wait(2)
        assert doors.http("system", "GET", "/v1/whoami")[0] == 200
        permit.set()
        assert warming.wait(2)
        assert doors.http("system", "GET", "/v1/whoami")[0] == 200
        warmed.set()
        assert _await_scan(doors, path) == (200, {"scan": "ready"})
    finally:
        permit.set()
        warmed.set()
        doors.close()


def test_the_named_pr_lookup_takes_what_is_left_of_the_shared_budget(tmp_path, monkeypatch):
    import subprocess

    from plangraph.keeper import context_sources, timing
    from plangraph.keeper.errors import Refused
    from plangraph.keeper.evidence_git import GitAdapter
    from test_keeper_close_merged_scan import _git, merged_repo

    repo = tmp_path / "repo"
    merged_repo(repo, [])
    _git(repo, "remote", "add", "origin", "https://github.com/acme/demo.git")
    adapter = GitAdapter({"demo": {"path": str(repo), "ref": "main"}})
    seen = []
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        if cmd[0] != "gh":
            return real_run(cmd, **kw)
        seen.append((cmd[0], kw["timeout"]))
        raise OSError("no gh in the test")

    monkeypatch.setattr(context_sources.subprocess, "run", fake_run)
    with timing.io_budget(1):
        assert context_sources.named_merge(adapter, "demo", "#7") is None
    assert [timeout for cmd, timeout in seen if cmd == "gh"] and all(
        timeout <= 1 for cmd, timeout in seen if cmd == "gh")
    calls = len(seen)
    with timing.io_budget(0):   # nothing is left: no gh call starts
        try:
            context_sources.named_merge(adapter, "demo", "#7")
        except Refused:
            pass
    assert len(seen) == calls
    assert context_sources.named_merge(adapter, "demo", "#7") is None and seen[-1] == ("gh", 20)
