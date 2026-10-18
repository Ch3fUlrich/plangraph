#!/usr/bin/env python3
"""RT-P1 end-to-end run against an already-running devstack (no pytest, standalone).

Proves the whole chain on the dev stack: a real Postgres keeper (items defined through the
real `pg define` CLI), the Rust worker on `temporal server start-dev`, a Card workflow that
asks a question, the projector mapping it to `needs_input`, a router answer signalled back
through the keeper, the merge on the phase branch, and the attempt rows read from the keeper DB.

Needs running: `tools/devstack.sh start` and `tools/devstack.sh keeper` (its keeper.json lives in
--state), plus a built pg-worker (--worker-bin / PG_WORKER_BIN) and the temporal CLI
(--temporal-cli / TEMPORAL_CLI). This script never starts or stops any of them. Everything is
loopback; the live keeper port 8787 is refused.

Front-only gap: the keeper has no HTTP door yet for /v1/attempts|park|complete|alarms|locks, so
the worker talks to the same VerbFront as the local e2e and the run prints the paths only the
front answered under 'front-only verbs (keeper has no door yet)'.

Usage: python3 tools/e2e_devstack.py [--leg stub|opencode] [--model <opencode model>] ...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "e2e"))   # reuse the local-e2e helpers, do not copy them
sys.path.insert(0, str(REPO / "src"))             # plangraph for rtp1_local_util + the read-back

import rtp1_local_util as u  # noqa: E402

PROJECT = "demo"
QUESTION_WAIT_S, WORKFLOW_WAIT_S = 90, 300
# Same rows the test helper attempts() reads; db_pg translates the '?' placeholders.
ATTEMPT_COLS = ("n", "exit_class", "cost", "tokens_in", "tokens_out", "harness", "model")


class Fail(Exception):
    """Any failed step exits 1 with this as the reason."""


def step(msg: str) -> None:
    print(f"[step] {msg}", flush=True)


def _hostport(addr: str, what: str) -> tuple[str, str]:
    host, sep, port = addr.rpartition(":")
    if not sep or not port.isdigit():
        raise Fail(f"{what} must be HOST:PORT (got {addr!r})")
    if host != "127.0.0.1":
        raise Fail(f"{what} must be loopback (refusing {addr!r})")
    return host, port


def _check_url(url: str, what: str, refuse_port: int | None = None) -> str:
    parsed = urllib.parse.urlparse(url if "//" in url else f"http://{url}")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise Fail(f"{what} is not a URL (got {url!r})")
    if parsed.hostname != "127.0.0.1":
        raise Fail(f"{what} must be loopback (refusing {url!r})")
    if refuse_port and parsed.port == refuse_port:
        raise Fail(f"{what} must not use the live keeper port {refuse_port}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _run(cmd: list[str], what: str, *, env=None, timeout=120) -> subprocess.CompletedProcess:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=REPO, env=env, stdin=subprocess.DEVNULL)
    except OSError as exc:
        raise Fail(f"{what}: {exc}") from exc
    if done.returncode != 0:
        raise Fail(f"{what} exited {done.returncode}: "
                   f"{(done.stderr or done.stdout)[-400:].strip()}")
    return done


def wait_until(predicate, what: str, timeout: float, step_s: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step_s)
    raise Fail(f"timeout waiting for {what}")


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
    token_file = scratch / "router.token"
    token_file.write_text(router_token, encoding="utf-8")
    os.chmod(token_file, 0o600)                 # the CLI only reads tokens from a file
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"), PG_URL=keeper,
               PG_TOKEN_FILE=str(token_file))
    _run([sys.executable, "-m", "plangraph.keeper.cli", "define", "--file", str(path)],
         "define", env=env)
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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--temporal", default=os.environ.get("TEMPORAL_ADDR", "127.0.0.1:7233"),
                   help="dev server HOST:PORT (env TEMPORAL_ADDR)")
    p.add_argument("--temporal-http", default=os.environ.get("PLANGRAPH_TEMPORAL_HTTP", ""),
                   help="Temporal HTTP API the keeper signals through (env PLANGRAPH_TEMPORAL_HTTP)")
    p.add_argument("--keeper", default="http://127.0.0.1:18787", help="dev keeper URL")
    p.add_argument("--state", default=os.environ.get("DEVSTACK_STATE", str(REPO / ".devstack")),
                   help="devstack state dir holding keeper.json (env DEVSTACK_STATE)")
    p.add_argument("--worker-bin", default=os.environ.get("PG_WORKER_BIN", ""),
                   help="built pg-worker (env PG_WORKER_BIN)")
    p.add_argument("--temporal-cli", default=os.environ.get("TEMPORAL_CLI") or shutil.which("temporal"),
                   help="temporal CLI binary (env TEMPORAL_CLI)")
    p.add_argument("--leg", choices=("stub", "opencode"), default="stub")
    p.add_argument("--model", default="", help="opencode model id for --leg opencode")
    p.add_argument("--keep", action="store_true", help="keep the scratch dir")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    keeper = _check_url(args.keeper, "--keeper", refuse_port=8787)
    _hostport(args.temporal, "--temporal")
    if args.temporal_http:
        _check_url(args.temporal_http, "--temporal-http")
    if args.leg == "opencode" and not args.model:
        raise Fail("--leg opencode needs --model <opencode model id>")
    cfg = Path(args.state) / "keeper.json"
    if not cfg.is_file():
        raise Fail(f"{cfg} missing — run tools/devstack.sh keeper first")
    worker_bin, cli = args.worker_bin, args.temporal_cli
    for what, path in (("--worker-bin", worker_bin), ("--temporal-cli", cli)):
        if not path or not Path(path).is_file():
            raise Fail(f"{what} is not an existing binary ({path or 'unset'})")

    pid = os.getpid()
    key, wid = f"{PROJECT}:DS-{pid}", f"card:{PROJECT}:DS-{pid}"
    build_id = f"e2e-devstack-{pid}"
    scratch = Path(tempfile.mkdtemp(prefix=f"e2e-devstack-{pid}-"))
    cleanup = []                                   # teardown stack, popped in reverse
    step(f"preflight: keeper {keeper}, temporal {args.temporal}, state {args.state}")
    try:
        try:                                   # alive without a token answers 401
            status, _ = u.call(keeper, "GET", "/v1/ready")
        except OSError as exc:
            raise Fail(f"dev keeper not reachable at {keeper}: {exc}")
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

        step(f"scratch repo + workroot under {scratch}")
        repo = scratch / "repo"
        base_sha = u.init_repo(repo)
        workroot = scratch / "workroot"
        workroot.mkdir()
        legs = [{"id": "leg-1", "tier": "free",
                 "model": args.model if args.leg == "opencode" else "some/model",
                 "harness": "opencode"}]
        if args.leg == "stub":
            u.write_fake_opencode(scratch / "stub-bin")   # asks PG_QUESTION, on the answer writes out.txt
        else:
            step("leg=opencode: the question step is skipped (that path needs a real model that "
                 "asks); this mode only requires the card to finish done/merged")

        step("start VerbFront + pg-worker against the dev keeper")
        front = u.VerbFront(keeper)
        cleanup.append(("front", front.stop))
        env = dict(os.environ, PG_TEMPORAL_ADDR=f"http://{args.temporal}", PG_BUILD_ID=build_id,
                   PG_NAMESPACE="default", PG_KEEPER_URL=front.base,
                   PG_KEEPER_TOKEN=tokens["worker"],
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
                   GIT_AUTHOR_NAME="worker", GIT_AUTHOR_EMAIL="w@example.com",
                   GIT_COMMITTER_NAME="worker", GIT_COMMITTER_EMAIL="w@example.com")
        if args.leg == "stub":                            # isolated HOME: the stub needs no config
            env["PATH"] = f"{scratch / 'stub-bin'}:{env['PATH']}"
            env["HOME"] = str(scratch)
        worker = u.WorkerProc(worker_bin, env, scratch / "worker.log")
        cleanup.append(("worker", worker.stop))
        try:
            worker.wait_up()
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
                    signal = (answered or {}).get("signal") or {}
                    if status != 200 or signal.get("status") != "sent":
                        raise Fail(f"answer_question: status {status}, signal {signal}")
                    step("router answered; keeper signalled the workflow")

                step(f"awaiting the workflow result (≤ {WORKFLOW_WAIT_S} s)")
                result = loop.run_until_complete(asyncio.wait_for(handle.result(),
                                                                  WORKFLOW_WAIT_S))
            finally:
                loop.close()
            if result["outcome"] != "done" or "merged" not in json.dumps(result):
                raise Fail(f"card outcome {result.get('outcome')!r}, expected done/merged")
            merged = u.git(repo, "show", f"{u.BRANCH}:{u.CARD_FILE}")   # raises if absent
            merged_sha = u.git(repo, "rev-parse", u.BRANCH)
            step(f"workflow done; {u.CARD_FILE} merged on {u.BRANCH} ({merged_sha[:9]})"
                 + (f", content {merged!r}" if args.leg == "stub" else ""))

            step("project again, then read the attempt rows back from the dev keeper")
            run_projector(cfg, args.temporal)
            rows = read_attempts(cfg, key)
            for row in rows:
                print("[step] attempt " + " ".join(
                    f"{c}={row[c]}" for c in ATTEMPT_COLS if c in row))
            if not any(a["exit_class"] == "ok" for a in rows):
                raise Fail(f"no attempt with exit_class ok: {rows}")
            print("[step] front-only verbs (keeper has no door yet)")
            print("[step] " + json.dumps(sorted(set(front.paths()) - set(u.FORWARDED))))
            print("RTP1 DEVSTACK RECORD " + json.dumps({
                "item": key, "workflow_id": wid, "leg": args.leg, "base_sha": base_sha,
                "merged_sha": merged_sha, "temporal_http": args.temporal_http,
                "attempts": [(a["n"], a["exit_class"]) for a in rows]}, sort_keys=True))
        finally:
            while cleanup:
                name, stop = cleanup.pop()
                try:
                    stop()
                except Exception as exc:               # noqa: BLE001 - keep tearing down
                    sys.stderr.write(f"teardown {name}: {exc!r}\n")
    except Fail as exc:
        print(f"E2E-DEVSTACK-FAIL: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:                              # noqa: BLE001 - one short reason
        print(f"E2E-DEVSTACK-FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)
        else:
            step(f"scratch kept under {scratch}")
    print("E2E-DEVSTACK-OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
