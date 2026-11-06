#!/usr/bin/env python3
"""RT-P1 end-to-end run against an already-running devstack (no pytest, standalone).

Proves the whole chain on the dev stack: a real Postgres keeper (items defined through the
real `pg define` CLI), the Rust worker on `temporal server start-dev`, a Card workflow that
asks a question, the projector mapping it to `needs_input`, a router answer signalled back
through the keeper, the merge on the phase branch, and the attempt rows read from the keeper DB.

Needs running: `tools/devstack.sh start` and `tools/devstack.sh keeper` (its keeper.json lives in
--state), plus a built pg-worker (--worker-bin / PG_WORKER_BIN) and the temporal CLI
(--temporal-cli / TEMPORAL_CLI). This script never starts or stops any of them. Everything is
loopback; `tools/devstack.sh status` prints the matching `export TEMPORAL_ADDR=...` and
`export PLANGRAPH_TEMPORAL_HTTP=...` for the devstack's own ports -- --temporal and --keeper
have no defaults and any port in REFUSED_PORTS (the live/default ones) is a hard error.

The worker talks straight to the keeper; the run reads the keeper audit_log back to show which
worker verbs (claim, attempt, park, complete, alarm, lock acquire/release) the real keeper served.

Usage: python3 tools/e2e_devstack.py [--leg stub|stub-fail|opencode] [--model <opencode model>] ...
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))            # adjacent standalone support module
sys.path.insert(0, str(REPO / "tests" / "e2e"))   # reuse the local-e2e helpers, do not copy them
sys.path.insert(0, str(REPO / "src"))             # plangraph for rtp1_local_util + the read-back

import rtp1_local_util as u  # noqa: E402
from e2e_devstack_support import (  # noqa: E402
    Fail, REFUSED_PORTS, _check_url, _hostport, _refuse_port, _run,
    assert_verbs, check_keeper_config, check_devstack, interrupted, parse_args, step, stop_worker,
    validate_args, wait_until, write_failing_opencode, write_stub_opencode,
)

PROJECT = "demo"
QUESTION_WAIT_S, WORKFLOW_WAIT_S = 90, 300
#: longer than the sum of QUESTION_WAIT_S + WORKFLOW_WAIT_S + projector waits (~400 s
#: worst case), so no heartbeat is needed; the assert below is the guard -- if those
#: waits are ever raised past the lease, the script must heartbeat (verb `heartbeat`,
#: body key+epoch).
CLAIM_LEASE_S = 1920  # main extends this past a larger --box-seconds, too
assert QUESTION_WAIT_S + WORKFLOW_WAIT_S + 120 < CLAIM_LEASE_S
# Same rows the test helper attempts() reads; db_pg translates the '?' placeholders.
ATTEMPT_COLS = ("n", "exit_class", "cost", "tokens_in", "tokens_out", "harness", "model")


def add_principal(cfg: Path, name: str, role: str, projects: str) -> str:
    """`admin add-principal` prints '<name> token (shown once): <token>'; keep it in memory only."""
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    cmd = [sys.executable, "-m", "plangraph.keeper.cli", "--config", str(cfg),
           "admin", "add-principal", name, "--role", role]
    if projects:
        cmd += ["--projects", projects]
    done = _run(cmd, f"add-principal {name}", env=env)
    match = re.search(r"token \(shown once\): (\S+)", done.stdout)
    if not match:
        raise Fail(f"add-principal {name}: token not in output shape")
    return match.group(1)


def claim_item(keeper: str, worker_token: str, key: str) -> int:
    """Claim the card as the worker principal over the real verb; returns the epoch."""
    status, body = u.call(keeper, "POST", "/v1/claim", token=worker_token,
                          body={"key": key, "lease_s": CLAIM_LEASE_S})
    epoch = (body or {}).get("epoch")
    if status != 200 or not isinstance(epoch, int):
        raise Fail(f"claim {key}: status {status}, no valid epoch")
    return epoch


def release_claim(keeper: str, worker_token: str, key: str, epoch: int) -> None:
    """Best effort at teardown: a refusal (the card already left `claimed`) is only logged."""
    status, body = u.call(keeper, "POST", "/v1/release", token=worker_token,
                          body={"key": key, "epoch": epoch})
    if status != 200:
        sys.stderr.write(f"teardown release: status {status} (a finished card is expected to refuse)\n")


def define_item(cfg: Path, keeper: str, router_token: str, key_id: str, scratch: Path) -> str:
    """The real `define` CLI over HTTP, with the item shape keeper_util.seed() builds
    (kind lane + a goal above it, so views.validate and ask_question accept it)."""
    item = {"project": PROJECT, "id": key_id, "title": f"e2e-devstack card {key_id}",
            "kind": "lane", "track": "T0", "owner": "e2e-devstack", "estimate_h": 1,
            "depends_on": [], "done_criteria": "card file merged",
            "evidence_kinds": ["any"], "operator": False}
    goal = dict(item, id=f"GOAL-{key_id}", kind="goal", depends_on=[f"{PROJECT}:{key_id}"])
    path = scratch / "define.json"
    path.write_text(json.dumps({"items": [item, goal]}), encoding="utf-8")
    token_fd = os.memfd_create("e2e-token")
    os.write(token_fd, router_token.encode())
    os.lseek(token_fd, 0, os.SEEK_SET)
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"), PG_URL=keeper,
               PG_TOKEN_FILE=f"/proc/self/fd/{token_fd}")
    try:
        _run([sys.executable, "-m", "plangraph.keeper.cli", "define", "--file", str(path)],
             "define", env=env, pass_fds=(token_fd,))
    finally:
        os.close(token_fd)
    return f"{PROJECT}:{key_id}"


def run_projector(cfg: Path, address: str) -> dict:
    env = dict(os.environ, PG_CONFIG=str(cfg), TEMPORAL_ADDRESS=address,
               PYTHONPATH=str(REPO / "src"))
    done = _run([sys.executable, "-m", "plangraph.projector", "run-once"], "projector run-once",
                env=env)
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise Fail(f"projector run-once: no JSON summary ({exc})") from exc


def read_attempts(cfg: Path, key: str) -> list[dict]:
    """The test helper attempts() reads, straight from the dev keeper's DB (works on Postgres:
    the ? placeholders are translated by db_pg; there is no /v1/attempts door yet)."""
    from plangraph.keeper import config as kconfig, db as kdb
    cfg_obj = kconfig.load_config(cfg)
    conn = kdb.open_db(cfg_obj.db, **cfg_obj.db_options)
    try:
        return [dict(r) for r in
                conn.execute("SELECT * FROM attempts WHERE key=? ORDER BY n", (key,))]
    finally:
        conn.close()


def read_audit(cfg: Path, actor: str) -> list[dict]:
    """The keeper's own audit trail for one principal, straight from the dev keeper's DB."""
    from plangraph.keeper import config as kconfig, db as kdb
    cfg_obj = kconfig.load_config(cfg)
    conn = kdb.open_db(cfg_obj.db, **cfg_obj.db_options)
    try:
        return [dict(r) for r in
                conn.execute("SELECT action, key, ok, code FROM audit_log WHERE actor=?"
                             " ORDER BY id", (actor,))]
    finally:
        conn.close()


def run(args) -> int:
    keeper, _http = validate_args(args)
    cfg = Path(args.state) / "keeper.json"
    if not cfg.is_file():
        raise Fail("keeper config missing")
    check_keeper_config(cfg)
    check_devstack(args, keeper, _http)
    worker_bin, cli = args.worker_bin, args.temporal_cli
    for what, path in (("--worker-bin", worker_bin), ("--temporal-cli", cli)):
        if not path or not Path(path).is_file():
            raise Fail(f"{what} is not an existing binary")

    pid = os.getpid()
    key, wid = f"{PROJECT}:DS-{pid}", f"card:{PROJECT}:DS-{pid}"
    build_id = f"e2e-devstack-{pid}"
    scratch = Path(tempfile.mkdtemp(prefix=f"e2e-devstack-{pid}-"))
    cleanup = []                                   # teardown stack, popped in reverse
    step("preflight: isolated loopback endpoints validated")
    try:
        try:                                   # alive without a token answers 401
            status, _ = u.call(keeper, "GET", "/v1/ready")
        except OSError as exc:
            raise Fail("dev keeper unreachable") from exc
        if status >= 500:
            raise Fail(f"dev keeper unhealthy at {keeper}: {status}")
        _run([cli, "operator", "cluster", "health", "--address", args.temporal],
             "temporal cluster health")

        step(f"mint throwaway principals on the dev keeper (project {PROJECT}, item {key})")
        names = {"worker": f"e2e-worker-{pid}", "router": f"e2e-router-{pid}",
                 "system": f"e2e-system-{pid}"}
        # system: the projector's own role per keeper_util.PRINCIPALS; worker scoped to the project.
        tokens = {role: add_principal(cfg, n, role, PROJECT if role == "worker" else "")
                  for role, n in names.items()}

        step(f"define the card item via the real CLI: {key}")
        define_item(cfg, keeper, tokens["router"], f"DS-{pid}", scratch)
        epoch = claim_item(keeper, tokens["worker"], key)
        cleanup.append(("release claim",
                        lambda: release_claim(keeper, tokens["worker"], key, epoch)))
        step(f"claimed {key} as {names['worker']} epoch {epoch}")

        step("initializing scratch repository and workroot")
        repo = scratch / "repo"
        base_sha = u.init_repo(repo)
        workroot = scratch / "workroot"
        workroot.mkdir()
        legs = [{"id": "leg-1", "tier": "free",
                 "model": args.model if args.leg == "opencode" else "some/model",
                 "harness": "opencode"}]
        if args.leg == "stub":
            write_stub_opencode(scratch / "stub-bin")
        elif args.leg == "stub-fail":
            write_failing_opencode(scratch / "stub-bin")  # always crashes -> worker_crash x3 -> parked
        else:
            step("leg=opencode: the question step is skipped (that path needs a real model that "
                 "asks); this mode only requires the card to finish done/merged")

        step("start pg-worker against the dev keeper")
        env = dict(os.environ, PG_TEMPORAL_ADDR=f"http://{args.temporal}", PG_BUILD_ID=build_id,
                   PG_NAMESPACE="default", PG_KEEPER_URL=keeper,
                   PG_KEEPER_TOKEN=tokens["worker"],
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
                   GIT_AUTHOR_NAME="worker", GIT_AUTHOR_EMAIL="w@example.com",
                   GIT_COMMITTER_NAME="worker", GIT_COMMITTER_EMAIL="w@example.com")
        if args.leg in ("stub", "stub-fail"):              # isolated HOME: the stubs need no config
            env["PATH"] = f"{scratch / 'stub-bin'}:{env['PATH']}"
            env["HOME"] = str(scratch)
        blocked = {}  # Python handlers run on the main thread, even for peer delivery.
        pending = []
        def defer(signum, frame):
            pending.append((signum, frame))
        try:
            for sig in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT):
                blocked[sig] = signal.signal(sig, defer)
            worker = u.WorkerProc(worker_bin, env, Path(os.devnull))
            worker.owned_pgid = worker.proc.pid  # WorkerProc creates a new session; do not reap early.
            cleanup.append(("worker", lambda: stop_worker(worker)))
        finally:
            for sig, handler in blocked.items():
                signal.signal(sig, handler)
            if pending:
                sig, frame = pending[0]
                handler = blocked[sig]
                if callable(handler):
                    handler(sig, frame)
                elif handler == signal.SIG_DFL:
                    signal.raise_signal(sig)
        try:
            u.set_current_version(cli, args.temporal, build_id)  # deployment pg-worker, unique build
            step(f"worker up, build {build_id} current")

            step(f"start Card workflow {wid} on pg-cards")
            card = {"claim": {"card": key, "repo": str(repo), "base_sha": base_sha,
                              "check_cmd": f"test -f {u.CARD_FILE}", "allow": [u.CARD_FILE],
                              "forbid": [], "prompt": "write out.txt"},
                    "legs": legs, "workroot": str(workroot), "main_branch": u.BRANCH,
                    "question_wait_cap_s": WORKFLOW_WAIT_S}
            loop = asyncio.new_event_loop()
            try:
                try:
                    from temporalio.client import Client   # lazy: only this phase needs it
                except ImportError:
                    raise Fail("temporalio python package is required (pip install temporalio)")
                client = loop.run_until_complete(Client.connect(args.temporal))
                handle = loop.run_until_complete(client.start_workflow(
                    "Card", card, id=wid, task_queue="pg-cards"))

                if args.leg == "stub":
                    def open_question():
                        status, body = u.call(keeper, "GET", f"/v1/questions?key={key}",
                                              token=tokens["router"])
                        row = body["questions"][0] if status == 200 and body["questions"] else None
                        return row if row and row["state"] == "open" else None

                    question = wait_until(open_question, "the worker's question row",
                                          QUESTION_WAIT_S)
                    step(f"question row {question['id']} open; projecting attempt 1")
                    run_projector(cfg, args.temporal)
                    # a persistent devstack keeps other runs, so check our own row instead
                    # of the test's global missing_item==0
                    wait_until(lambda: [a for a in read_attempts(cfg, key) if a["n"] == 1],
                               "attempt 1 projected", 30)
                    status, answered = u.call(keeper, "POST", "/v1/answer_question",
                                              token=tokens["router"],
                                              body={"question_id": question["id"],
                                                    "text": f"use {u.ANSWER_TOKEN}"})
                    answer_signal = (answered or {}).get("signal") or {}
                    if status != 200 or answer_signal.get("status") != "sent":
                        raise Fail(f"answer_question: status {status}, signal not sent")
                    step("router answered; keeper signalled the workflow")

                step("awaiting the workflow result within the run time box")
                result = loop.run_until_complete(asyncio.wait_for(handle.result(),
                                                                  args.box_seconds))
            finally:
                loop.close()
            if args.leg == "stub-fail":
                # the failing stub crashed every retry, so the card parked with an alarm and
                # never merged: there is no merge commit to read on this leg
                if result["outcome"] != "parked":
                    raise Fail(f"card outcome {result.get('outcome')!r}, expected parked")
                merged_sha = ""
                step(f"workflow finished; outcome {result['outcome']} (no merge on this leg)")
            else:
                if result.get("outcome") != "done" or result.get("detail") != "merged":
                    raise Fail("workflow_not_merged")
                u.git(repo, "show", f"{u.BRANCH}:{u.CARD_FILE}")   # raises if absent
                merged_sha = u.git(repo, "rev-parse", u.BRANCH)
                if merged_sha == base_sha or result.get("merged_sha") != merged_sha:
                    raise Fail("merge_sha_mismatch")
                step(f"workflow done; {u.CARD_FILE} merged on {u.BRANCH} ({merged_sha[:9]})")

            step("project again, then read the attempt rows back from the dev keeper")
            run_projector(cfg, args.temporal)
            rows = read_attempts(cfg, key)
            pairs = [(a["n"], a["exit_class"]) for a in rows]
            if args.leg == "stub":
                if pairs != [(1, "needs_input"), (2, "ok")]:
                    raise Fail(f"attempt pairs {pairs}, expected [(1, 'needs_input'), (2, 'ok')]")
            elif args.leg == "stub-fail":
                if pairs != [(1, "worker_crash"), (2, "worker_crash"), (3, "worker_crash")]:
                    raise Fail(f"attempt pairs {pairs}, expected worker_crash x3")
            elif not any(a["exit_class"] == "ok" for a in rows):
                raise Fail("no_ok_attempt")
            audit = read_audit(cfg, names["worker"])
            for row in audit:
                print(f"[step] audit action={row['action']} key={row['key']} "
                      f"ok={row['ok']} code={row['code']}")
            seen = sorted({r["action"] for r in audit if r["ok"]})
            print("[step] worker verbs served by the keeper: " + json.dumps(seen))
            if args.leg == "stub-fail":
                assert_verbs(seen, ("claim", "worker_attempt", "worker_park", "worker_alarm"),
                             forbidden=("worker_complete", "lock_acquire"))
            else:
                assert_verbs(seen, ("claim", "worker_attempt", "worker_complete",
                                    "lock_acquire", "lock_release"))
            print("RTP1 DEVSTACK RECORD " + json.dumps({
                "item": key, "workflow_id": wid, "leg": args.leg, "base_sha": base_sha,
                "merged_sha": merged_sha, "temporal_http_configured": bool(_http),
                "outcome": result["outcome"], "epoch": epoch,
                "worker_verbs": seen,
                "attempts": pairs}, sort_keys=True))
            if args.leg == "stub-fail":
                raise Fail("workflow_parked")
        finally:
            primary_failure = sys.exc_info()[0] is not None
            teardown_failed = False
            teardown_reason = None
            while cleanup:
                name, stop = cleanup.pop()
                try:
                    stop()
                except Exception as exc:               # noqa: BLE001 - keep tearing down
                    teardown_failed = True
                    if isinstance(exc, Fail) and teardown_reason is None:
                        teardown_reason = exc
                    sys.stderr.write(f"teardown {name}: {type(exc).__name__}\n")
            if teardown_failed and not primary_failure:
                raise teardown_reason or Fail("teardown_failed")
    except Fail as exc:
        print(f"E2E-DEVSTACK-FAIL: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:                              # noqa: BLE001 - one short reason
        print(f"E2E-DEVSTACK-FAIL: {type(exc).__name__} (details withheld)", file=sys.stderr)
        return 1
    finally:
        while cleanup:
            name, stop = cleanup.pop()
            try:
                stop()
            except Exception as exc:               # noqa: BLE001 - keep tearing down
                sys.stderr.write(f"teardown {name}: {type(exc).__name__}\n")
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            step("scratch retained")
    print("E2E-DEVSTACK-OK")
    return 0


def main() -> int:
    args = parse_args()
    previous = {}
    try:
        validate_args(args)  # refuse before any keeper write or signal setup
        global CLAIM_LEASE_S
        CLAIM_LEASE_S = max(CLAIM_LEASE_S, args.box_seconds + 120)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM):
            previous[sig] = signal.signal(sig, interrupted)
        signal.setitimer(signal.ITIMER_REAL, args.box_seconds)
        return run(args)
    except Fail as exc:
        print(f"E2E-DEVSTACK-FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        if previous:
            signal.setitimer(signal.ITIMER_REAL, 0)
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
