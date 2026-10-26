"""The whole RT-P1 flow, locally: keeper, Rust worker, Temporal dev server, projector, inbox, review.

Skipped unless ``TEMPORAL_CLI`` (temporal binary) and ``PG_WORKER_BIN`` (built ``pg-worker``) are set.
Everything binds 127.0.0.1 on free ports.  The keeper backend follows ``PG_KEEPER_BACKEND`` like the
other keeper tests.  The same steps are the live-run script once a cloud cluster is reachable
(``docs/plan/rtp1-live-run.md``).

Flow: dev server -> keeper -> worker -> Card workflow
-> worker asks a question -> projector -> router answers (signal over the Temporal HTTP API) ->
second attempt merges -> projector again -> rebuild equality -> review leg over the phase diff.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

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
from keeper_util import World, seed  # noqa: E402
from projector_live_util import DevServer, ScopedSource, wait_for  # noqa: E402
from test_projector_live import assert_rebuild_equal, attempts, item_state, ledger  # noqa: E402

from plangraph.projector.temporal_source import TemporalSource  # noqa: E402
from plangraph.review import actions, family, seat  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ITEM, WID = "demo:E2E-1", "card:demo:E2E-1"
BUILD_ID = "e2e-local"
FIXTURES = TESTS / "fixtures" / "review"
#: The live-set fallback the worker sends when it observes no state; with a
#: readable keeper it parks/asks with the observed state instead.  ``waiting``
#: is in the set: the projector writes waiting after usage_limit/outage and on
#: a timer, so the cap-park must still land there.
WORKER_LIVE_SET = ("open", "ready", "running", "needs_input", "waiting")


def reply(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def run_projector(cfg: Path, address: str) -> dict:
    env = dict(os.environ, PG_CONFIG=str(cfg), TEMPORAL_ADDRESS=address,
               PYTHONPATH=str(ROOT / "src"))
    done = subprocess.run([sys.executable, "-m", "plangraph.projector", "run-once"], env=env,
                          capture_output=True, text=True, timeout=120, cwd=ROOT)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture
def rig(tmp_path, monkeypatch, request):
    """Dev server, keeper, worker; torn down in reverse; logs stay under tmp_path."""
    started = []                                     # cleanup stack: (name, stop)

    def push(name, stop):
        started.append((name, stop))

    class Rig:
        pass

    r = Rig()
    r.tmp, r.t0 = tmp_path, time.monotonic()
    try:
        r.dev = DevServer(_CLI, tmp_path, http_api=True)
        r.dev.start()
        push("temporal", r.dev.stop)
        r.addr = r.dev.address
        r.http = f"http://127.0.0.1:{r.dev.http_port}"
        monkeypatch.setenv("PLANGRAPH_TEMPORAL_HTTP", r.http)   # the keeper signals through this
        monkeypatch.delenv("PLANGRAPH_TEMPORAL_NAMESPACE", raising=False)

        r.world = World(tmp_path / "keeper-a")
        push("world", r.world.close)
        seed(r.world, [("E2E-1", [])])
        r.initial_state = request.param
        if r.initial_state == "ready":
            r.world.conn.execute("UPDATE items SET state='ready' WHERE key=?", (ITEM,))
            r.world.conn.commit()
        r.keeper = u.KeeperServer(r.world, tmp_path)
        push("keeper", r.keeper.stop)

        r.repo = tmp_path / "repo"
        r.base_sha = u.init_repo(r.repo)
        r.workroot = tmp_path / "workroot"
        r.workroot.mkdir()
        bin_dir = tmp_path / "stub-bin"
        u.write_fake_opencode(bin_dir)
        env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(tmp_path),
               "PG_TEMPORAL_ADDR": f"http://{r.addr}", "PG_BUILD_ID": BUILD_ID, "PG_NAMESPACE": "default",
               "PG_KEEPER_URL": r.keeper.base, "PG_KEEPER_TOKEN": r.world.tok("workerA"),
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_AUTHOR_NAME": "worker", "GIT_AUTHOR_EMAIL": "w@example.com",
               "GIT_COMMITTER_NAME": "worker", "GIT_COMMITTER_EMAIL": "w@example.com"}
        r.worker = u.WorkerProc(_BIN, env, tmp_path / "worker.log")
        push("worker", r.worker.stop)
        r.worker.wait_up()
        u.set_current_version(_CLI, r.addr, BUILD_ID)
        yield r
    except BaseException:
        sys.stderr.write(f"\n--- e2e logs under {tmp_path} ---\n")
        for name in ("worker.log", "temporal.log"):
            f = tmp_path / name
            if f.is_file():
                sys.stderr.write(f"[{name} tail]\n{f.read_text(errors='replace')[-2500:]}\n")
        raise
    finally:
        for _, stop in reversed(started):
            try:
                stop()
            except Exception as exc:                 # noqa: BLE001 - keep tearing down
                sys.stderr.write(f"teardown: {exc!r}\n")


def card_input(r) -> dict:
    return {"claim": {"card": ITEM, "repo": str(r.repo), "base_sha": r.base_sha,
                      "check_cmd": f"test -f {u.CARD_FILE}", "allow": [u.CARD_FILE],
                      "forbid": [], "prompt": "write out.txt"},
            "legs": [{"id": "leg-1", "tier": "free", "model": "some/model", "harness": "opencode"}],
            "workroot": str(r.workroot), "main_branch": u.BRANCH, "question_wait_cap_s": 300}


@pytest.mark.parametrize("rig", ["open", "ready"], indirect=True)
def test_rtp1_local_end_to_end(rig, tmp_path):
    try:
        _flow(rig, tmp_path)
    except BaseException:
        sys.stderr.write("\n" + u.failure_report(tmp_path, _CLI, rig.addr, WID) + "\n")
        raise


def _flow(rig, tmp_path):
    r, world, timings = rig, rig.world, {}
    router = world.tok("router")
    cfg = r.keeper.cfg_path

    def mark(name):
        timings[name] = round(time.monotonic() - r.t0, 2)

    mark("stack_up")
    # No synthetic running state: worker_attempt is a read-only check and the
    # worker's ask/park expectation (observed state, WORKER_LIVE_SET fallback,
    # waiting included) must accept the seeded pre-projection card.
    assert item_state(world, ITEM) == r.initial_state
    assert r.initial_state in WORKER_LIVE_SET
    loop = asyncio.new_event_loop()
    client = loop.run_until_complete(Client.connect(r.addr))
    handle = loop.run_until_complete(client.start_workflow(
        "Card", card_input(r), id=WID, task_queue="pg-cards"))

    # 5. the worker asks over HTTP; the row lands in the real keeper
    def open_question():
        status, body = u.call(r.keeper.base, "GET", f"/v1/questions?key={ITEM}", token=router)
        return body["questions"][0] if status == 200 and body["questions"] else None

    question = wait_for(open_question, "the worker's question row", timeout=90)
    mark("question_row")
    assert question["state"] == "open" and question["default_answer"] is None

    # Wait for the question park before projecting, so both worker calls exercise
    # the real report -> ask -> park -> projector order (not a projected running).
    wait_for(lambda: item_state(world, ITEM) == "parked",
             "the worker's pre-projection question park", timeout=90)

    first = run_projector(cfg, r.addr)
    # one unmapped run is the dev server's own readiness probe (no plan key), never a card
    assert first["missing_item"] == 0 and first["unmapped"] <= 1, first
    rows = attempts(world, ITEM)
    assert [(a["n"], a["exit_class"]) for a in rows] == [(1, "needs_input")], rows
    assert item_state(world, ITEM) == "needs_input"
    status, wm = u.call(r.keeper.base, "GET", "/v1/projector_watermark", token=router)
    assert status == 200 and wm["open_alarms"] == 0, (json.dumps(u.call(r.keeper.base, "GET", "/v1/alarms", token=router)))
    assert [w["plan_key"] for w in wm["workflows"]] == [ITEM] and wm["global"], wm
    status, alarms = u.call(r.keeper.base, "GET", "/v1/alarms", token=router)
    assert status == 200 and alarms["alarms"] == [], alarms        # no unknown_shape
    ledger_after_first = ledger(world)
    mark("projected_needs_input")

    # 6. the router answers; the keeper signals the workflow over the Temporal HTTP API
    status, answered = u.call(r.keeper.base, "POST", "/v1/answer_question", token=router,
                              body={"question_id": question["id"],
                                    "text": f"use {u.ANSWER_TOKEN}"})
    assert status == 200 and answered["signal"]["status"] == "sent", answered
    assert answered["signal"]["workflow_id"] == WID
    result = loop.run_until_complete(asyncio.wait_for(handle.result(), 120))
    mark("workflow_done")
    assert result["outcome"] == "done" and "merged" in json.dumps(result), result
    assert u.git(r.repo, "show", f"{u.BRANCH}:{u.CARD_FILE}") == "done"    # merged into the phase branch
    merged_sha = u.git(r.repo, "rev-parse", u.BRANCH)
    assert merged_sha != r.base_sha

    # 7. projector again: forward only, attempt 2 ok, same rows when rebuilt on a fresh keeper
    run_projector(cfg, r.addr)
    rows = attempts(world, ITEM)
    assert [(a["n"], a["exit_class"]) for a in rows] == [(1, "needs_input"), (2, "ok")], rows
    assert set(ledger_after_first) <= set(ledger(world))                      # nothing rewritten, only added
    assert item_state(world, ITEM) == "running"                               # done belongs to the keeper
    again = run_projector(cfg, r.addr)
    assert again["attempts_written"] == again["states_written"] == 0, again   # idempotent
    status, wm = u.call(r.keeper.base, "GET", "/v1/projector_watermark", token=router)
    assert wm["open_alarms"] == 0 and wm["workflows"][0]["status"].upper() == "COMPLETED", wm
    source = ScopedSource(TemporalSource(address=r.addr), [WID])
    assert_rebuild_equal(world, source, [ITEM], tmp_path)
    mark("rebuilt_equal")

    audited = {row[0] for row in world.conn.execute(
        "SELECT action FROM audit_log WHERE ok=1 AND action IN (?,?,?,?,?)",
        ("ask_question", "worker_attempt", "worker_complete", "lock_acquire", "lock_release")).fetchall()}
    assert audited == {"ask_question", "worker_attempt", "worker_complete", "lock_acquire", "lock_release"}, audited
    refused = world.conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE ok=0 AND action IN (?,?,?,?,?,?)",
        ("worker_attempt", "worker_park", "worker_complete", "worker_alarm", "lock_acquire", "lock_release")).fetchone()[0]
    assert refused == 0, refused
    locks = world.conn.execute("SELECT token, owner FROM merge_locks").fetchall()
    assert len(locks) == 1 and locks[0][0] >= 1 and locks[0][1] == "", [tuple(x) for x in locks]   # taken, then released

    # 8. review leg over the phase diff (recorded replies, no network)
    diff = u.git(r.repo, "diff", r.base_sha, u.BRANCH)
    assert f"+++ b/{u.CARD_FILE}" in diff
    review = review_leg(tmp_path / "review", diff)
    mark("review_done")

    print("\nRTP1 LOCAL RUN RECORD " + json.dumps({
        "workflow_id": WID, "run_id": handle.result_run_id, "item": ITEM,
        "question_id": question["id"], "answer_signal": answered["signal"],
        "attempts": [(a["n"], a["exit_class"]) for a in rows], "merged_sha": merged_sha,
        "keeper_backend": os.environ.get("PG_KEEPER_BACKEND", "sqlite"),
        "keeper_audited_verbs": sorted(audited),
        "review": review, "timings_s": timings, "logs": str(tmp_path)}, sort_keys=True))
    loop.close()


def review_leg(out: Path, diff: str) -> dict:
    """Pass (standard risk), pass after a 429 on the anthropic leg, and a fix reply."""
    from plangraph.review import gateway, opencode, qoder

    def gw(text, status=200, finish="stop", error=None):
        return gateway.CallResult(
            status=status, text=text, finish_reason=finish, served_model="m", requested_model="m",
            correlation_id="c", response_header_names=[], tokens={}, prompt_sha256="ab" * 32,
            started="s", finished="f", error=error, session_tag="t", run_id="r")

    def qo(text):
        return qoder.QoderResult(rc=0, text=text, finish_reason="stop", num_turns=1, is_error=False,
                                 model="Qwen3.8-Flash", prompt_sha256="ab" * 32, started="s",
                                 finished="f", error=None, usage={}, rate_limited=False)

    def oc(text):
        return opencode.OpencodeResult(rc=0, text=text, finish_reason="stop",
                                       model="opencode/deepseek-v3", prompt_sha256="ab" * 32,
                                       started="s", finished="f", error=None)

    def never(*a, **k):
        raise AssertionError("leg must not be called")

    def spec(name, **kw):
        return seat.SeatSpec(key=ITEM, writer_family="openai", vectors=["does the card do its job"],
                             files={}, diff=diff, out_root=out / name, **kw)

    assert len(family.families_for_risk("standard", "openai")) == 1
    standard = seat.run_seat(spec("standard"), gateway_call=never,
                             qoder_run=lambda p, model=None: qo(reply("ok_pass.txt")),
                             opencode_run=never)
    plan = actions.plan_actions(standard[-1].verdict, ITEM)
    assert seat.overall(standard) == {"qwen": "pass"} and plan["pass"] and plan["fix_cards"] == []

    seen = []

    def gateway_call(model, prompt_text, title=None):
        seen.append(model)
        if model.startswith("claude"):
            return gw("", status=429, finish=None, error="HTTP 429: rate limit")
        return gw(reply("ok_pass.txt"))

    retried = seat.run_seat(spec("retry", down=frozenset({"qwen"})), gateway_call=gateway_call,
                            qoder_run=never, opencode_run=lambda p, model=None: oc(reply("ok_pass.txt")))
    plan = actions.plan_actions(retried[-1].verdict, ITEM)
    assert seen and retried[-1].family != "anthropic" and retried[-1].tried[0] == "anthropic"
    assert seat.overall(retried) == {"anthropic": "incomplete", "opencode": "pass"}
    assert plan["pass"] and plan["fix_cards"] == []

    fixed = seat.run_seat(spec("fix"), gateway_call=never,
                          qoder_run=lambda p, model=None: qo(reply("fix_with_findings.txt")),
                          opencode_run=never)
    plan = actions.plan_actions(fixed[-1].verdict, ITEM)
    assert seat.overall(fixed) == {"qwen": "fix"} and not plan["pass"] and plan["fix_cards"]
    return {"standard": "pass", "anthropic_429_then": retried[-1].leg, "fix_cards": len(plan["fix_cards"])}
