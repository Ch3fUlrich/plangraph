"""The Phase runner end-to-end against the real keeper, worker, git, and fake GitHub REST.

Skipped unless ``TEMPORAL_CLI`` (temporal binary) and ``PG_WORKER_BIN`` (built ``pg-worker``) are set.
Loopback only; the keeper is the real threaded API over a sqlite ``World``.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import pytest

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest.importorskip("temporalio")

_CLI, _BIN = os.environ.get("TEMPORAL_CLI"), os.environ.get("PG_WORKER_BIN")
pytestmark = pytest.mark.skipif(
    not (_CLI and Path(_CLI).is_file() and _BIN and Path(_BIN).is_file()),
    reason="set TEMPORAL_CLI (temporal binary) and PG_WORKER_BIN (built pg-worker)")

from temporalio.client import Client  # noqa: E402

import rtp1_local_util as u  # noqa: E402
from keeper_util import World, defn  # noqa: E402
from projector_live_util import DevServer  # noqa: E402

from plangraph.keeper import api_routes  # noqa: E402
from plangraph.planner.github_adapter import RestGitHub  # noqa: E402

ITEM = "demo:PH-X"
WID = "phase-demo-PH-X"
BUILD_ID = "e2e-local"
FIXTURES = TESTS / "fixtures" / "review"
PHASE_BRANCH = "phase/demo-PH-X"
PHASE_REF = "refs/heads/" + PHASE_BRANCH

def reply(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")

def _status_name(status) -> str:
    return getattr(status, "name", str(status))

def _write_opencode(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / "opencode"
    exe.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then\n'
                   '  echo "opencode 2.0.32"\n  exit 0\nfi\n'
                   'for last; do :; done\necho done > "$last"\n', encoding="utf-8")
    exe.chmod(0o755)

def _payload(body):
    if body is None:
        return {}
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    if isinstance(body, str):
        return json.loads(body) if body else {}
    return body

async def _run_phase(r):
    client = await Client.connect(r.addr)
    handle = await client.start_workflow(
        "Phase",
        {
            "key": ITEM,
            "repo": str(r.repo),
            "workroot": str(r.workroot),
            "base_branch": "main",
            "max_parallel": 2,
            "legs": [{"id": "opencode-glm-5.3-flash-free", "tier": "free", "model": "some/model",
                      "harness": "opencode"}],
            "finish_poll_s": 1,
            "finish_max_polls": 10,
        },
        id=WID,
        task_queue="pg-cards",
    )
    desc = await handle.describe()
    result = await handle.result()
    children = []
    for key in ('demo:C1','demo:C2'):
        child_id = f"card:{ITEM}:{key}:1"
        child = client.get_workflow_handle(child_id)
        children.append((child_id, await child.describe()))
    return result, desc.run_id, children

def _review_leg(out: Path, diff: str) -> dict:
    from plangraph.review import actions, gateway, qoder, seat

    def gw(text, status=200, finish="stop", error=None):
        return gateway.CallResult(
            status=status, text=text, finish_reason=finish, served_model="m",
            requested_model="m", correlation_id="c", response_header_names=[],
            tokens={}, prompt_sha256="ab" * 32, started="s", finished="f",
            error=error, session_tag="t", run_id="r")

    def qo(text):
        return qoder.QoderResult(
            rc=0, text=text, finish_reason="stop", num_turns=1, is_error=False,
            model="Qwen3.8-Flash", prompt_sha256="ab" * 32, started="s",
            finished="f", error=None, usage={}, rate_limited=False)

    def never(*a, **k):
        raise AssertionError("leg must not be called")

    def spec(name, **kw):
        return seat.SeatSpec(
            key=ITEM, writer_family="openai",
            vectors=["does the card do its job"], files={}, diff=diff,
            out_root=out / name, **kw)

    passed = seat.run_seat(
        spec("pass"), gateway_call=never,
        qoder_run=lambda p, model=None: qo(reply("ok_pass.txt")),
        opencode_run=never)
    pass_plan = actions.plan_actions(passed[-1].verdict, ITEM)
    assert pass_plan["pass"] and pass_plan["fix_cards"] == []

    fixed = seat.run_seat(
        spec("fix"), gateway_call=never,
        qoder_run=lambda p, model=None: qo(reply("fix_with_findings.txt")),
        opencode_run=never)
    fix_plan = actions.plan_actions(fixed[-1].verdict, ITEM)
    assert not fix_plan["pass"] and fix_plan["fix_cards"]
    return {"pass": bool(pass_plan["pass"]), "fix_cards": len(fix_plan["fix_cards"])}

@pytest.fixture
def rig(tmp_path, monkeypatch, request):
    """Dev server, real keeper, worker; torn down in reverse; logs stay under tmp_path."""
    started = []                                     # cleanup stack: (name, stop)

    def push(name, stop):
        started.append((name, stop))

    class Rig:
        pass

    r = Rig()
    r.conclusion = getattr(request, "param", "success")
    r.finishes = []
    from plangraph.planner import phase_finish
    real_finish = phase_finish.finish_phase

    def record_finish(*args, **kwargs):
        result = real_finish(*args, **kwargs)
        with sqlite3.connect(r.world.path) as db:
            evidence = db.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        r.finishes.append((dict(result), len(r.merge_bodies), evidence))
        return result

    monkeypatch.setattr(phase_finish, "finish_phase", record_finish)
    r.tmp, r.t0 = tmp_path, time.monotonic()
    try:
        r.dev = DevServer(_CLI, tmp_path, http_api=True)
        r.dev.start()
        push("temporal", r.dev.stop)
        r.addr = r.dev.address
        r.http = f"http://127.0.0.1:{r.dev.http_port}"
        monkeypatch.setenv("PLANGRAPH_TEMPORAL_HTTP", r.http)
        monkeypatch.delenv("PLANGRAPH_TEMPORAL_NAMESPACE", raising=False)

        r.world = World(tmp_path / "keeper")
        push("world", r.world.close)
        r.world.store.create_items(r.world.p("router"), [
            defn("C1", project="demo", kind="lane"),
            defn("C2", project="demo", kind="lane"),
            defn("PH-X", deps=["C1", "C2"], project="demo", kind="gate"),
            defn("GOAL", deps=["PH-X"], project="demo", kind="goal"),
        ])
        r.world.store.put_card(r.world.p("router"), "demo:C1",
                               {"outcome": "a.txt", "check_cmd": "test -f a.txt",
                                "write_set": ["a.txt"],
                                "window_class": "128k", "estimate_tokens": 1000})
        r.world.store.put_card(r.world.p("router"), "demo:C2",
                               {"outcome": "b.txt", "check_cmd": "test -f b.txt",
                                "write_set": ["b.txt"],
                                "window_class": "128k", "estimate_tokens": 1000})

        r.repo = tmp_path / "repo"
        r.repo.mkdir()
        u.git(r.repo, "init", "-q", "-b", "main")
        (r.repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        u.git(r.repo, "add", "-A")
        u.git(r.repo, "commit", "-q", "-m", "init")
        r.base_sha = u.git(r.repo, "rev-parse", "HEAD")

        r.bare = tmp_path / "remote.git"
        u.git(tmp_path, "init", "--bare", "-q", str(r.bare))

        token_file = tmp_path / "github-token"
        token_file.write_text("tok" + "en-" + "value", encoding="utf-8")
        token_file.chmod(0o600)

        calls = []
        merge_bodies = []
        check_polls = [0]
        pr_holder = {"pr": None}

        def fake_opener(method: str, url: str, headers, body):
            parsed = urlparse(url)
            path = parsed.path
            calls.append((method, path))
            if method == "GET" and path.endswith("/pulls") and "head=" in parsed.query:
                pr = pr_holder["pr"]
                return 200, ([] if pr is None else [pr])
            if method == "POST" and path.endswith("/pulls"):
                sha = u.git(r.repo, "rev-parse", PHASE_REF)
                pr = {
                    "number": 5,
                    "html_url": "https://example.invalid/pr/5",
                    "state": "open",
                    "merged_at": None,
                    "head": {"sha": sha},
                }
                pr_holder["pr"] = pr
                return 201, pr
            if method == "GET" and path.endswith("/check-runs"):
                check_polls[0] += 1
                sha = (path.split("/commits/", 1)[1].split("/check-runs", 1)[0]
                       if "/commits/" in path else pr_holder["pr"]["head"]["sha"])
                assert sha == pr_holder["pr"]["head"]["sha"]
                if check_polls[0] <= 2:
                    return 200, {"check_runs": [], "total_count": 0}
                return 200, {
                    "check_runs": [{
                        "id": 1,
                        "name": "ci-gate",
                        "status": "completed",
                        "conclusion": r.conclusion,
                        "head_sha": sha,
                    }],
                    "total_count": 1,
                }
            if method == "PUT" and path.endswith("/pulls/5/merge"):
                data = _payload(body)
                merge_bodies.append(data)
                assert r.conclusion == "success"
                assert data["sha"] == pr_holder["pr"]["head"]["sha"]
                assert data["merge_method"] == "merge"
                assert data["commit_title"] == "Merge phase demo:PH-X"
                assert [line for line in data["commit_message"].splitlines()
                        if line.startswith("Plan-Item:")] == [
                            "Plan-Item: demo:C1", "Plan-Item: demo:C2"]
                u.git(r.repo, "checkout", "main")
                u.git(r.repo, "merge", "--no-ff", "-m", data["commit_title"],
                      "-m", data["commit_message"], data["sha"])
                sha = u.git(r.repo, "rev-parse", "HEAD")
                pr_holder["pr"].update(state="closed", merged_at="2026-10-24T00:00:00Z",
                                       merge_commit_sha=sha)
                return 200, {"merged": True, "sha": sha}
            if method == "GET" and path.endswith("/pulls/5"):
                return 200, pr_holder["pr"]
            raise AssertionError(f"unexpected GitHub call {method} {url}")

        monkeypatch.setenv("PG_PHASE_REPO_ROOT", str(r.repo))
        monkeypatch.setenv("PG_PHASE_BASE_BRANCH", "main")
        monkeypatch.setattr(
            api_routes,
            "_github_from_env",
            lambda: RestGitHub("owner/name", token_file, remote=str(r.bare),
                               opener=fake_opener),
        )
        r.calls, r.merge_bodies, r.check_polls = calls, merge_bodies, check_polls

        r.keeper = u.KeeperServer(r.world, tmp_path)
        push("keeper", r.keeper.stop)

        r.workroot = tmp_path / "workroot"
        r.workroot.mkdir()
        bin_dir = tmp_path / "stub-bin"
        _write_opencode(bin_dir)
        env = {
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path),
            "PG_TEMPORAL_ADDR": f"http://{r.addr}",
            "PG_BUILD_ID": BUILD_ID,
            "PG_NAMESPACE": "default",
            "PG_KEEPER_URL": r.keeper.base,
            "PG_KEEPER_TOKEN": r.world.tok("system"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "worker",
            "GIT_AUTHOR_EMAIL": "w@example.com",
            "GIT_COMMITTER_NAME": "worker",
            "GIT_COMMITTER_EMAIL": "w@example.com",
        }
        r.worker = u.WorkerProc(_BIN, env, tmp_path / "worker.log")
        push("worker", r.worker.stop)
        r.worker.wait_up()
        u.set_current_version(_CLI, r.addr, BUILD_ID)
        yield r
    except BaseException:
        sys.stderr.write(f"\n--- e2e logs under {tmp_path} ---\n")
        raise
    finally:
        for name, stop in reversed(started):
            try:
                stop()
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the test failure
                sys.stderr.write(f"cleanup {name} failed: {exc!r}\n")

def test_phase_runs_two_cards_to_merge_through_the_real_keeper(rig):
    _assert_phase_chain(rig)


@pytest.mark.parametrize("rig", ["failure"], indirect=True)
def test_phase_red_ci_gate_never_merges(rig):
    _assert_phase_chain(rig)


def _assert_phase_chain(rig):
    r = rig
    result, run_id, children = asyncio.run(_run_phase(r))

    assert result["outcome"] == ("done" if r.conclusion == "success" else "partial"), result
    assert sorted(result["merged"]) == ["demo:C1", "demo:C2"], repr(result)
    assert result["not_merged"] == []
    assert result["waves"] == 1
    assert result["branch"] == PHASE_BRANCH
    assert result["finish"] is not None
    waiting = [entry for entry in r.finishes if entry[0]["state"] == "waiting_ci"]
    assert len(waiting) >= 2
    assert all(puts == 0 and evidence == 0 for _, puts, evidence in waiting)
    head = u.git(r.repo, "rev-parse", PHASE_REF)
    assert u.git(r.bare, "show-ref").splitlines() == [f"{head} {PHASE_REF}"]
    if r.conclusion == "success":
        assert result["finish"]["state"] == "merged", result
        assert len(r.merge_bodies) == 1
        merge_sha = u.git(r.repo, "rev-parse", "main")
        assert u.git(r.repo, "rev-list", "--parents", "-n", "1", merge_sha).split() == [
            merge_sha, r.base_sha, head]
    else:
        assert result["finish"]["state"] == "waiting_ci", result
        assert r.merge_bodies == []
        assert not [c for c in r.calls if c[0] == "PUT"]
        assert u.git(r.repo, "rev-parse", "main") == r.base_sha

    import json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        r.keeper.base + "/v1/phase_finish",
        data=json.dumps({
            "key": ITEM,
            "branch": PHASE_BRANCH,
            "head": u.git(r.repo, "rev-parse", PHASE_REF),
        }).encode(),
        headers={
            "Authorization": "Bearer " + r.world.tok("system"),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with sqlite3.connect(r.world.path) as db:
        before = [db.execute(f"SELECT * FROM {table}").fetchall()
                  for table in ("evidence", "events")]
    with urllib.request.urlopen(req) as response:
        assert response.status == 200
    with sqlite3.connect(r.world.path) as db:
        assert before == [db.execute(f"SELECT * FROM {table}").fetchall()
                          for table in ("evidence", "events")]

    for child_id, desc in children:
        assert _status_name(desc.status).endswith("COMPLETED"), child_id
        assert desc.parent_id == WID, child_id

    tree = set(u.git(r.repo, "ls-tree", "-r", "--name-only", PHASE_REF).splitlines())
    assert {"a.txt", "b.txt", "seed.txt"} <= tree

    with sqlite3.connect(r.world.path) as db:
        claim_count = db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='claim' AND ok=1").fetchone()[0]
        cols = {row[1] for row in db.execute("PRAGMA table_info(items)")}
        key_col = "key" if "key" in cols else "item"
        rows = db.execute(
            f"SELECT {key_col}, claimed_by, state FROM items WHERE {key_col} IN (?, ?)",
            ("demo:C1", "demo:C2"),
        ).fetchall()
        for key in ("demo:C1", "demo:C2"):
            audit = db.execute(
                "SELECT action, actor, role FROM audit_log WHERE key=? AND ok=1 ORDER BY id",
                (key,)).fetchall()
            claim = audit.index(("claim", "system", "system"))
            complete = audit.index(("worker_complete", "system", "system"))
            assert claim < complete
            events = db.execute("SELECT payload FROM events WHERE key=? AND type='claimed'",
                                (key,)).fetchall()
            assert len(events) == 1
            assert json.loads(events[0][0])["via"] == "worker_claim"
            proof = db.execute("SELECT kind, ref FROM evidence WHERE key=?", (key,)).fetchall()
            assert proof == ([("commit+ci", json.dumps({"commit": merge_sha},
                                                      sort_keys=True, separators=(",", ":")))]
                             if r.conclusion == "success" else [])
    assert claim_count == 2
    assert len(rows) == 2
    assert all(by == "system" and state == "in_review" for _, by, state in rows)

    diff = u.git(r.repo, "diff", r.base_sha, PHASE_BRANCH)
    review = _review_leg(r.tmp / "review", diff)

    record = {
        "workflow_id": WID,
        "run_id": run_id,
        "claim_count": claim_count,
        "review": review,
    }
    print("PHASE LOCAL RUN RECORD " + json.dumps(record, sort_keys=True))
