"""MD-SYNC-SNAPSHOT-TIMEOUT: keeper snapshot reads get their own 60 s budget.

The keeper "snapshot" GET is large; ``Session.snapshot()`` must reach
``opener.open`` with ``SNAPSHOT_TIMEOUT`` (60 s), never the generic 15 s, on
both the md_sync import path and the md_sync_archive path. The unit tests pin
the Session plumbing; the path tests spy on the read that really happens.
"""
import io
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import apply_batch  # noqa: E402
import md_sync  # noqa: E402

from keeper_util import World, defn  # noqa: E402
from plangraph.keeper import api, config  # noqa: E402
from plangraph.keeper.evidence_decisions import DecisionsAdapter  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "md_sync"


@pytest.fixture
def site(tmp_path):
    """A stand-in for the router checkout: the batch root the keeper trusts."""
    site = tmp_path / "site"
    site.mkdir()
    renamed = {"QUESTIONS.md": "sample_questions.md",
               "ANSWERS.md": "sample_answers.md"}
    for name in ("QUESTIONS.md", "ANSWERS.md", "decisions-log.md"):
        shutil.copy(FIX / renamed.get(name, name), site / name)
    return site


@pytest.fixture
def world(tmp_path, site):
    w = World(tmp_path)
    r = w.p("router")
    w.store.create_items(r, [
        defn("R-1", project="routing"),
        defn("Q-SHARED", project="server"),
        defn("GOAL", project="routing", kind="goal",
             deps=["routing:R-1", "server:Q-SHARED"])])
    w.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(site / "decisions-log.md"))
    w.store.batch_roots = [str(site)]
    yield w
    w.close()


@pytest.fixture
def srv(world, tmp_path):
    cfg = tmp_path / "keeper.json"
    cfg.write_text(json.dumps({"db": str(world.path), "bind": "127.0.0.1:0",
                               "snapshot": str(tmp_path / "plan.snapshot.json")}))
    os.chmod(cfg, 0o600)
    server = api.make_server(world.store, config.load_config(cfg), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def token(world, tmp_path):
    path = tmp_path / "router.token"
    path.write_text(world.tokens["router"], encoding="utf-8")
    return str(path)


def sync(site, srv, token, *extra, answers="ANSWERS.md"):
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(["--questions", str(site / "QUESTIONS.md"),
                       "--answers", str(site / answers),
                       "--decisions", str(site / "decisions-log.md"),
                       "--url", srv.base_url, "--token-file", token, *extra],
                      out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


class _FakeResp:
    """Context-managed response body for the recording opener below."""
    def __init__(self, payload=b"{}"):
        self._payload = payload
    def read(self):
        return self._payload
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False


class _RecordingOpener:
    """Fake opener: records every open() timeout and answers an empty JSON dict."""
    def __init__(self):
        self.timeouts = []
    def open(self, request, timeout=None):
        self.timeouts.append(timeout)
        return _FakeResp()


def _recording_session():
    session = apply_batch.Session("http://127.0.0.1:1", "tok")
    session._opener = _RecordingOpener()
    return session


def test_snapshot_timeout_is_passed_to_the_opener():
    # MD-SYNC-SNAPSHOT-TIMEOUT: the large snapshot GET must reach opener.open
    # with its own budget, not the generic 15 s.
    session = _recording_session()
    session.snapshot()
    assert apply_batch.SNAPSHOT_TIMEOUT == 60
    assert session._opener.timeouts == [apply_batch.SNAPSHOT_TIMEOUT]


def test_plain_get_and_post_keep_the_15_second_default():
    session = _recording_session()
    session.get("snapshot")
    session.post("priority", {"key": "demo:B", "priority": 1})
    assert apply_batch.TIMEOUT == 15
    assert session._opener.timeouts == [15, 15]


def test_snapshot_read_uses_the_snapshot_timeout(world, srv, token, site, monkeypatch):
    """MD-SYNC-SNAPSHOT-TIMEOUT: the large keeper snapshot GET gets its own budget."""
    seen = []
    orig_get = md_sync.apply_batch.Session.get
    def spy(self, verb, **kw):
        seen.append((verb, kw.get("timeout")))
        return orig_get(self, verb, **kw)
    monkeypatch.setattr(md_sync.apply_batch.Session, "get", spy)
    sync(site, srv, token, "--dry-run")
    reads = [t for verb, t in seen if verb == "snapshot"]
    assert reads and set(reads) == {md_sync.apply_batch.SNAPSHOT_TIMEOUT}
    assert md_sync.apply_batch.SNAPSHOT_TIMEOUT == 60


def test_archive_snapshot_read_uses_the_snapshot_timeout(tmp_path, monkeypatch):
    """MD-SYNC-SNAPSHOT-TIMEOUT: the archive path's snapshot GET carries the same budget.

    The archive fakes elsewhere swallow **kw, so silently dropping the timeout
    kwarg there stays green; this spy asserts the kwarg itself.
    """
    seen = []
    def spy(self, verb, **kw):
        seen.append((verb, kw.get("timeout")))
        return {"items": [], "aliases": {}}
    monkeypatch.setattr(md_sync.apply_batch.Session, "get", spy)
    tok = tmp_path / "tok"
    tok.write_text("x" * 20, encoding="utf-8")
    os.chmod(tok, 0o600)
    out, err = io.StringIO(), io.StringIO()
    rc = md_sync.main(["--archive-answers", "--questions-out", str(tmp_path / "QUESTIONS.md"),
                       "--url", "http://keeper.invalid", "--token-file", str(tok),
                       "--apply"], out=out, err=err)
    assert rc == 0, out.getvalue() + err.getvalue()
    reads = [t for verb, t in seen if verb == "snapshot"]
    assert reads and set(reads) == {md_sync.apply_batch.SNAPSHOT_TIMEOUT}
    assert md_sync.apply_batch.SNAPSHOT_TIMEOUT == 60


def test_unreachable_snapshot_read_keeps_unreachable_in_the_status_error(world, srv, token,
                                                                        site, tmp_path,
                                                                        monkeypatch):
    """Preservation guard (passes on base too, not failing-first): an unreachable
    snapshot read keeps 'unreachable' in the status-file error."""
    status = tmp_path / "status.json"
    def down(self, verb, **kw):
        raise md_sync.apply_batch.ApiError("unreachable", "TimeoutError")
    monkeypatch.setattr(md_sync.apply_batch.Session, "get", down)   # no socket, no port race
    rc, out, err = sync(site, srv, token, "--status-file", str(status))
    assert rc == 2
    assert "cannot read the live graph: unreachable" in err
    doc = json.loads(status.read_text(encoding="utf-8"))
    assert doc["error"].startswith("error: cannot read the live graph: unreachable")
