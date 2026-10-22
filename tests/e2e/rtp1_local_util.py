"""Helpers for the local RT-P1 end-to-end test (not collected by pytest).

Pieces: the keeper HTTP server over a seeded ``World`` (the worker talks to it directly; it serves
the verbs and the locks itself), a PATH-stubbed fake ``opencode``, the Rust worker as a subprocess,
a fixture git repo, and small HTTP/JSON helpers.  Loopback only.
"""
from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from plangraph.keeper import api, config

ANSWER_TOKEN = "ANSWER-PURPLE-42"       # appears only in the human answer, never in the card prompt
BRANCH = "phase-rtp1"                   # the phase branch the card merges into
CARD_FILE = "out.txt"

FAKE_OPENCODE = """#!/bin/sh
# argv: run -m <model> <prompt>.  The prompt only carries the answer on the resumed attempt.
case "$*" in
  *{token}*)
    printf 'done\\n' > {card_file}
    echo "wrote {card_file}"
    ;;
  *)
    echo 'PG_QUESTION: {"text":"Which colour should out.txt use?","default":null,"deadline_s":null,"risk_tags":[]}'
    ;;
esac
""".replace("{token}", ANSWER_TOKEN).replace("{card_file}", CARD_FILE)


def call(base: str, method: str, path: str, *, token: str | None = None, body=None, timeout=15):
    """One JSON request to the keeper; returns ``(status, decoded_json)``."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw}


# ------------------------------------------------------------------ keeper
class KeeperServer:
    """The real threaded keeper API over a ``World`` store, on a free loopback port."""

    def __init__(self, world, tmp: Path) -> None:
        self.world = world
        self.cfg_path = Path(tmp) / "keeper.json"
        self.cfg_path.write_text(json.dumps({
            "db": str(world.path), "snapshot": str(Path(tmp) / "plan.snapshot.json"),
            "bind": "127.0.0.1:0", "reap_interval": 3600}), encoding="utf-8")
        os.chmod(self.cfg_path, 0o600)
        self.cfg = config.load_config(self.cfg_path)
        self.server = api.make_server(world.store, self.cfg, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


# ------------------------------------------------------------------ git + worker
def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_TERMINAL_PROMPT="0")
    out = subprocess.run(
        ["git", "-c", "user.name=tester", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert out.returncode == 0, f"git {args} failed: {out.stderr}"
    return out.stdout.strip()


def init_repo(path: Path) -> str:
    """A repo whose only branch is the phase branch, with one commit; returns the base sha."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", BRANCH)
    (path / "seed.txt").write_text("seed\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return git(path, "rev-parse", "HEAD")


def write_fake_opencode(bin_dir: Path) -> None:
    """Temp name + rename so the worker never execs a file that is still open for write."""
    bin_dir.mkdir(parents=True)
    tmp = bin_dir / "opencode.tmp"
    tmp.write_text(FAKE_OPENCODE)
    tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR)
    os.replace(tmp, bin_dir / "opencode")


class WorkerProc:
    """``pg-worker`` as a subprocess with its own session; log kept for the failure message."""

    def __init__(self, binary: str, env: dict, log: Path) -> None:
        env = env.copy()
        if "PG_JAIL_BIN" not in env and "PG_JAIL" not in env:
            jail = Path(binary).resolve().with_name("pg-jail")
            if jail.is_file():
                env["PG_JAIL_BIN"] = str(jail.resolve())
            else:
                # Test profile only: production startup must still fail closed.
                if os.environ.get("CI"):
                    raise RuntimeError(f"pg-jail helper missing next to {binary}: refusing an unjailed e2e run in CI")
                env["PG_JAIL"] = "off"
                env["PG_TEST_ALLOW_UNJAILED"] = "1"
        self.log = log
        self.out = open(log, "wb")
        self.proc = subprocess.Popen([binary], env=env, stdout=self.out, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)

    def text(self) -> str:
        return self.log.read_text(errors="replace")

    def wait_up(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"worker exited early:\n{self.text()[-2000:]}")
            if "worker up on task queue" in self.text():
                return
            time.sleep(0.2)
        raise RuntimeError(f"worker did not come up:\n{self.text()[-2000:]}")

    def stop(self) -> None:
        import signal
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
                self.proc.wait(timeout=10)
        self.out.close()


def set_current_version(cli: str, address: str, build_id: str, deployment: str = "pg-worker",
                        timeout: float = 60.0) -> None:
    """Make the worker's build the deployment's current version (it runs with worker versioning on,
    so a workflow only reaches it once the server routes the deployment to that build)."""
    deadline, last = time.monotonic() + timeout, ""
    while time.monotonic() < deadline:
        done = subprocess.run(
            [cli, "worker", "deployment", "set-current-version", "--address", address,
             "--deployment-name", deployment, "--build-id", build_id, "--yes"],
            capture_output=True, text=True, timeout=30)
        if done.returncode == 0:
            return
        last = done.stderr or done.stdout
        time.sleep(1.0)                      # the version registers when the worker first polls
    raise RuntimeError(f"set-current-version failed: {last[-500:]}")


def failure_report(tmp: Path, cli: str, address: str, workflow_id: str) -> str:
    """Log tails plus the workflow's history, for the failure message (server still up)."""
    parts = [f"e2e logs stay under {tmp}"]
    for name in ("worker.log", "temporal.log"):
        f = Path(tmp) / name
        if f.is_file():
            parts.append(f"[{name} tail]\n{f.read_text(errors='replace')[-2500:]}")
    try:
        shown = subprocess.run([cli, "workflow", "show", "--address", address, "--workflow-id",
                                workflow_id], capture_output=True, text=True, timeout=30)
        parts.append(f"[workflow show]\n{(shown.stdout or shown.stderr)[-1500:]}")
        raw = subprocess.run([cli, "workflow", "show", "--address", address, "--workflow-id",
                              workflow_id, "--output", "json"], capture_output=True, text=True,
                             timeout=30).stdout
        for ev in json.loads(raw).get("events", []):
            if ev.get("eventType") == "EVENT_TYPE_ACTIVITY_TASK_COMPLETED":
                for pl in ev["activityTaskCompletedEventAttributes"].get("result", {}).get("payloads", []):
                    text = base64.b64decode(pl.get("data", "")).decode("utf-8", "replace")
                    if "exit_class" in text:
                        parts.append("[attempt result] " + text[:600])
            if ev.get("eventType") == "EVENT_TYPE_ACTIVITY_TASK_FAILED":
                failure = ev["activityTaskFailedEventAttributes"]["failure"]
                parts.append("[activity failed] " + json.dumps(failure)[:400])
    except Exception as exc:  # noqa: BLE001 - the report itself must not hide the failure
        parts.append(f"[workflow show failed: {exc!r}]")
    return "\n".join(parts)
