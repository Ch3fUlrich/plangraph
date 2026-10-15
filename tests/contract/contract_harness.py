"""Black-box harness for the keeper contract suite.

Two modes, chosen by the environment:

* ``PG_CONTRACT_URL`` set: talk to that already running keeper.  Tokens come
  from ``PG_CONTRACT_TOKEN_<NAME>`` (see ``PRINCIPALS`` for the names, e.g.
  ``PG_CONTRACT_TOKEN_ROUTER``).  Optional server-side fixtures are announced
  with ``PG_CONTRACT_CAPS`` (comma list of ``decisions``, ``runs``, ``batch``,
  ``admin``) plus ``PG_CONTRACT_BATCH_DIR`` (a directory the keeper lists in
  its batch roots and this process can write), ``PG_CONTRACT_RUNS_DIR`` (the
  keeper's run-record directory) and ``PG_CONTRACT_ADMIN_CONFIG`` (a config
  for ``pg admin verify``).  Tests needing a missing capability are skipped.
* otherwise: start the Python keeper daemon as a subprocess on a temp dir and
  a free localhost port, seeded through the public ``pg admin`` CLI.

``PG_CONTRACT_START_TIMEOUT`` (seconds, default 60) bounds the wait for a
locally started keeper to become healthy.

Nothing here imports keeper internals; every assertion goes over HTTP or a
CLI subprocess.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

#: Per-request socket timeout (seconds) for every HTTP call to the keeper.
REQUEST_TIMEOUT = 20


def _start_timeout() -> float:
    """Seconds to wait for a started keeper to answer ``/v1/health``.

    ``PG_CONTRACT_START_TIMEOUT`` overrides the default of 60 (a Postgres or
    Rust keeper may need longer to migrate or compile on first start).
    """
    return float(os.environ.get("PG_CONTRACT_START_TIMEOUT", "60"))

#: name -> (role, projects, family).  A remote keeper must provide the same.
PRINCIPALS = {
    "router": ("router", [], None),
    "lead": ("l1", ["demo"], "famA"),
    "lead_other": ("l1", ["other"], "famA"),
    "worker1": ("worker", ["demo"], "famB"),
    "worker2": ("worker", ["demo"], "famB"),
    "reviewer1": ("reviewer", ["demo"], "famC"),
    "reviewer2": ("reviewer", ["demo"], "famR"),
    "reviewer_same": ("reviewer", ["demo"], "famB"),
    "reviewer_nofam": ("reviewer", ["demo"], None),
    "reader": ("reader", [], "famD"),
    "spawner": ("spawner", ["demo"], None),
}

#: The decisions file the keeper under test must read (capability ``decisions``).
DECISIONS = (
    "| D-1 | demo | A plain recorded decision. |\n"
    "| D-2 | demo | Closes: demo:CLOSE-1 demo:ANS-1 demo:WD-1 |\n"
    "| D-3 | demo | Waives: demo:HIGH-1 |\n"
)


#: (method, "/v1/<verb>") pairs this process called with a token that got past
#: authentication and routing; the coverage test reads it.
CALLED = set()


@dataclass
class Resp:
    status: int
    headers: dict
    raw: bytes

    @property
    def json(self):
        return json.loads(self.raw.decode("utf-8"))

    @property
    def text(self):
        return self.raw.decode("utf-8")

    @property
    def ctype(self):
        return self.headers.get("Content-Type", "")


@dataclass
class Keeper:
    url: str
    tokens: dict
    caps: set = field(default_factory=set)
    local: bool = False
    dir: Path | None = None          # local mode only
    config: Path | None = None       # config usable with ``pg admin``
    batch_dir: Path | None = None
    runs_dir: Path | None = None
    proc: object = None

    # ------------------------------------------------------------- HTTP
    def call(self, method, path, who=None, body=None, raw_body=None,
             token=None, headers=None):
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method=method)
        tok = token if token is not None else (self.tokens[who] if who else None)
        if tok:
            req.add_header("Authorization", f"Bearer {tok}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            req.add_header(name, value)
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                out = Resp(resp.status, dict(resp.headers), resp.read())
        except urllib.error.HTTPError as exc:
            out = Resp(exc.code, dict(exc.headers), exc.read())
        if out.status not in (401, 404, 405):     # the route ran past authentication
            CALLED.add((method, "/" + "/".join(path.split("?")[0].split("/")[1:3])))
        return out

    def get(self, path, who=None, **kw):
        return self.call("GET", path, who, **kw)

    def post(self, verb, who, body=None, **kw):
        return self.call("POST", f"/v1/{verb}", who,
                         body if body is not None else {}, **kw)

    # -------------------------------------------------------------- CLI
    def admin(self, *args):
        """Run ``pg admin <args>`` against this keeper's config."""
        if self.config is None:
            raise RuntimeError("no admin config for this keeper")
        return run_cli(["--config", str(self.config), "admin", *args])

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None


def _env(extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra or {})
    return env


def run_cli(args, env=None, cwd=None):
    return subprocess.run([sys.executable, "-m", "plangraph.keeper.cli", *args],
                          capture_output=True, text=True, timeout=60,
                          env=_env(env), cwd=cwd)


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _token_of(out: str) -> str:
    return out.strip().rsplit(":", 1)[1].strip()


def _path(var):
    return Path(os.environ[var]) if os.environ.get(var) else None


def from_env():
    """A handle on an external keeper, or ``None`` when not configured."""
    url = os.environ.get("PG_CONTRACT_URL")
    if not url:
        return None
    tokens = {}
    for name in PRINCIPALS:
        value = os.environ.get(f"PG_CONTRACT_TOKEN_{name.upper()}")
        if value:
            tokens[name] = value
    caps = {c for c in os.environ.get("PG_CONTRACT_CAPS", "").split(",") if c}
    return Keeper(url.rstrip("/"), tokens, caps, False, None,
                  _path("PG_CONTRACT_ADMIN_CONFIG"),
                  _path("PG_CONTRACT_BATCH_DIR"), _path("PG_CONTRACT_RUNS_DIR"))


def start_local(base: Path, signing: bool = False) -> Keeper:
    """Seed a fresh DB through ``pg admin`` and run the daemon on a free port.

    ``signing=True`` also generates an ed25519 key pair and enables signed snapshots.
    """
    base.mkdir(parents=True, exist_ok=True)
    batch_dir, runs_dir = base / "batches", base / "runs"
    batch_dir.mkdir()
    runs_dir.mkdir()
    (base / "DECISIONS.md").write_text(DECISIONS, encoding="utf-8")
    cfg = base / "keeper.json"
    cfg.write_text(json.dumps({
        "db": str(base / "keeper.db"), "snapshot": str(base / "plan.snapshot.json"),
        "decisions_file": str(base / "DECISIONS.md"), "runs_dir": str(runs_dir),
        "batch_roots": [str(batch_dir)], "reap_interval": 3600,
    }), encoding="utf-8")
    cfg.chmod(0o600)
    if signing:
        priv, pub = base / "snap.key", base / "snap.pub"
        keygen = run_cli(["--config", str(cfg), "admin", "snapshot-keygen",
                          "--private", str(priv), "--public", str(pub)])
        assert keygen.returncode == 0, keygen.stderr
        doc = json.loads(cfg.read_text())
        doc.update(signing_key=str(priv), snapshot_pubkey=str(pub))
        cfg.write_text(json.dumps(doc), encoding="utf-8")
    tokens = {}
    out = run_cli(["--config", str(cfg), "admin", "init", "--router", "router"])
    assert out.returncode == 0, out.stderr
    tokens["router"] = _token_of(out.stdout.splitlines()[-1])
    for name, (role, projects, family) in PRINCIPALS.items():
        if name == "router":
            continue
        args = ["--config", str(cfg), "admin", "add-principal", name,
                "--role", role, "--projects", ",".join(projects)]
        if family:
            args += ["--family", family]
        out = run_cli(args)
        assert out.returncode == 0, out.stderr
        tokens[name] = _token_of(out.stdout)
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "plangraph.keeper.daemon", "--config", str(cfg),
         "--host", "127.0.0.1", "--port", str(port)],
        env=_env(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    keeper = Keeper(f"http://127.0.0.1:{port}", tokens,
                    {"decisions", "runs", "batch", "admin"}, True, base, cfg,
                    batch_dir, runs_dir, proc)
    deadline = time.time() + _start_timeout()
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("keeper exited: " + proc.stderr.read().decode()[:500])
        try:
            if keeper.get("/v1/health").status == 200:
                return keeper
        except OSError:
            time.sleep(0.05)
    keeper.stop()
    raise RuntimeError(f"keeper did not come up within {_start_timeout():g}s "
                       "(PG_CONTRACT_START_TIMEOUT)")
