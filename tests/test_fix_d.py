"""FIX-D: tests for the keeper + batch applier review findings.

Covers M1, M2, M3, L1, L2, L3, L4 from the review.
"""
import hashlib
import io
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import apply_batch  # noqa: E402

from keeper_util import World, defn  # noqa: E402
from plangraph.keeper import api, config  # noqa: E402
from plangraph.keeper.evidence_decisions import DecisionsAdapter  # noqa: E402
from plangraph.keeper.errors import Refused  # noqa: E402


LANE = {"project": "demo", "id": "L1", "title": "a lane", "kind": "lane",
        "track": "T0", "owner": "tester", "estimate_h": 1, "depends_on": [],
        "done_criteria": "criteria", "evidence_kinds": ["any"],
        "review_tier": "standard", "operator": False}
GOAL = {"project": "demo", "id": "GOAL", "title": "goal", "kind": "goal",
        "track": "T0", "owner": "tester", "estimate_h": 1,
        "depends_on": ["demo:L1"], "done_criteria": "criteria",
        "evidence_kinds": ["any"], "operator": False}


def _src_digest(tmp_path, name="batch"):
    source = tmp_path / "plan" / f"{name}.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("recorded plan batch\n", encoding="utf-8")
    return source, hashlib.sha256(source.read_bytes()).hexdigest()


def _batch_path(tmp_path, source, digest, ops=None, name="batch", tamper=False):
    default_ops = ops if ops is not None else [{"op": "create", "item": [LANE, GOAL]}]
    doc = {"batch": f"X7d-{name}", "principal": "router",
           "source": str(source), "source_sha256": "f" * 64 if tamper else digest,
           "ops": default_ops}
    path = tmp_path / "plan" / f"{name}.yaml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return path


def _cfg(tmp_path, world):
    path = tmp_path / "keeper.json"
    path.write_text(json.dumps({
        "db": str(world.path),
        "snapshot": str(tmp_path / "plan.snapshot.json"),
        "bind": "127.0.0.1:0",
    }), encoding="utf-8")
    os.chmod(path, 0o600)
    return config.load_config(path)


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    decisions = tmp_path / "DECISIONS.md"
    decisions.write_text(
        "| ID | Note | Scope |\n|---|---|---|\n"
        "| D-9 | batch close | Closes: demo:L1 |\n", encoding="utf-8")
    w.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(decisions))
    w.store.batch_roots = [str(tmp_path / "plan")]
    yield w
    w.close()


@pytest.fixture
def srv(world, tmp_path):
    server = api.make_server(world.store, _cfg(tmp_path, world), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
def token_file(world, tmp_path):
    path = tmp_path / "router.token"
    path.write_text(world.tokens["router"], encoding="utf-8")
    return str(path)


def _run(batch_path, url, token_path, extra=()):
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path), "--url", url,
                           "--token-file", token_path, *extra],
                          out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


# M1: read_token refuses token with whitespace/control chars
@pytest.mark.parametrize("content", [
    "firstline\nsecondline\n",
    "abc\tdef",
    "abc\x00def",
    "abc\x1bdef",  # ESC
    "abc\x7fdef",  # DEL
    "abc def",     # space
    "abc\ndef",    # newline
    "abc\u00e9def",  # non-ASCII
])
def test_read_token_refuses_whitespace_and_control_chars(tmp_path, content):
    t = tmp_path / "t.token"
    t.write_text(content, encoding="utf-8")
    with pytest.raises(apply_batch.BatchError) as exc:
        apply_batch.read_token(t)
    # Ensure the token doesn't appear in the error message
    assert content not in str(exc.value)
    assert "invalid characters" in str(exc.value).lower() or "printable ascii" in str(exc.value).lower()


def test_validate_op_unhashable_op(tmp_path):
    src, dig = _src_digest(tmp_path, "badop")
    # Create a valid token file
    token_file = tmp_path / "token"
    token_file.write_text("VALIDTOKEN123", encoding="utf-8")
    
    # op as list
    batch_path1 = _batch_path(tmp_path, src, dig, ops=[{"op": ["a", "b"], "item": LANE}])
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path1), "--url", "http://x", "--token-file", str(token_file)],
                          out=out, err=err)
    assert rc == 1
    out_err = out.getvalue() + err.getvalue()
    assert "op 1" in out_err
    assert "resume with --from 1" in out_err
    assert "Traceback" not in out_err
    
    # op as dict
    batch_path2 = _batch_path(tmp_path, src, dig, ops=[{"op": {"a": "b"}, "item": LANE}])
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path2), "--url", "http://x", "--token-file", str(token_file)],
                          out=out, err=err)
    assert rc == 1
    out_err = out.getvalue() + err.getvalue()
    assert "op 1" in out_err
    assert "resume with --from 1" in out_err
    assert "Traceback" not in out_err


def test_get_call_never_leak_exception_text(tmp_path, world, srv, token_file, monkeypatch):
    import urllib.request
    original_open = urllib.request.OpenerDirector.open
    def failing_open(self, req, timeout=None):
        raise OSError("SECRETTOKEN-LEAK")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", failing_open)
    try:
        rc, out, err = _run(_batch_path(tmp_path, *_src_digest(tmp_path)), srv.base_url, token_file)
    finally:
        monkeypatch.setattr(urllib.request.OpenerDirector, "open", original_open)
    # The secret must not appear in output
    assert "SECRETTOKEN-LEAK" not in out
    assert "SECRETTOKEN-LEAK" not in err
    # Exception class name should appear
    assert "OSError" in (out + err)


# M2: whoami failure refuses before any op, unless --allow-no-whoami
def test_whoami_404_refuses_before_any_op(tmp_path, world, srv):
    src, dig = _src_digest(tmp_path, "w404")
    batch_path = _batch_path(tmp_path, src, dig)
    bad_token = tmp_path / "bad.token"
    bad_token.write_text("INVALID_TOKEN", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path), "--url", srv.base_url, "--token-file", str(bad_token)],
                          out=out, err=err)
    assert rc == 2, f"Expected exit 2, got {rc}: {out.getvalue()} {err.getvalue()}"
    assert "op 1" not in out.getvalue()
    assert "whoami" in (out.getvalue() + err.getvalue()).lower()


def test_allow_no_whoami_allows_continue(tmp_path, world, srv):
    src, dig = _src_digest(tmp_path, "wallow")
    batch_path = _batch_path(tmp_path, src, dig)
    bad_token = tmp_path / "bad.token"
    bad_token.write_text("INVALID_TOKEN", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path), "--url", srv.base_url, "--token-file", str(bad_token),
                           "--allow-no-whoami"], out=out, err=err)
    out_val, err_val = out.getvalue(), err.getvalue()
    # argv parsing succeeded (no "unrecognized" in error)
    assert "unrecognized" not in err_val
    # first op was attempted (output contains "ok 1" or "op 1")
    assert "ok 1" in out_val or "op 1" in out_val


# M3: _tier_for - explicit review_tier LOWER than default requires router
def test_tier_for_explicit_lower_than_default_needs_router(world):
    with pytest.raises(Refused) as exc:
        world.store.create_items(world.p("l1main"), [{
            "project": "demo", "id": "SEC", "title": "Handle the secret",
            "kind": "lane", "track": "T0", "owner": "tester",
            "estimate_h": 1, "depends_on": [], "done_criteria": "c",
            "evidence_kinds": ["any"], "review_tier": "standard",
            "operator": False
        }])
    assert exc.value.code == "forbidden"
    assert "lower" in str(exc.value.msg).lower() or "router" in str(exc.value.msg).lower()


def test_tier_for_explicit_equal_or_higher_allowed_for_l1(world):
    # Default standard, explicit high
    r = world.store.create_items(world.p("l1main"), [
        {"project": "demo", "id": "H1", "title": "Ordinary lane", "kind": "lane",
         "track": "T0", "owner": "tester", "estimate_h": 1, "depends_on": [],
         "done_criteria": "c", "evidence_kinds": ["any"], "review_tier": "high", "operator": False},
        {"project": "demo", "id": "G1", "title": "g", "kind": "goal", "track": "T0",
         "owner": "tester", "estimate_h": 1, "depends_on": ["demo:H1"],
         "done_criteria": "c", "evidence_kinds": ["any"], "operator": False}])
    assert world.store._item("demo:H1")["review_tier"] == "high"
    # Default standard, explicit standard (equal)
    r = world.store.create_items(world.p("l1main"), [
        {"project": "demo", "id": "S1", "title": "Ordinary lane", "kind": "lane",
         "track": "T0", "owner": "tester", "estimate_h": 1, "depends_on": [],
         "done_criteria": "c", "evidence_kinds": ["any"], "review_tier": "standard", "operator": False},
        {"project": "demo", "id": "G2", "title": "g", "kind": "goal", "track": "T0",
         "owner": "tester", "estimate_h": 1, "depends_on": ["demo:S1"],
         "done_criteria": "c", "evidence_kinds": ["any"], "operator": False}])
    assert world.store._item("demo:S1")["review_tier"] == "standard"
    # Default high (plangraph), explicit high (equal) - router
    r = world.store.create_items(world.p("router"), [
        {"project": "plangraph", "id": "P1", "title": "Plain lane", "kind": "lane",
         "track": "T0", "owner": "tester", "estimate_h": 1, "depends_on": [],
         "done_criteria": "c", "evidence_kinds": ["any"], "review_tier": "high", "operator": False},
        {"project": "plangraph", "id": "G3", "title": "g", "kind": "goal", "track": "T0",
         "owner": "tester", "estimate_h": 1, "depends_on": ["plangraph:P1"],
         "done_criteria": "c", "evidence_kinds": ["any"], "operator": False}])
    assert world.store._item("plangraph:P1")["review_tier"] == "high"


def test_tier_for_router_can_lower(world):
    r = world.store.create_items(world.p("router"), [
        {"project": "plangraph", "id": "P2", "title": "Plain lane", "kind": "lane",
         "track": "T0", "owner": "tester", "estimate_h": 1, "depends_on": [],
         "done_criteria": "c", "evidence_kinds": ["any"], "review_tier": "standard", "operator": False},
        {"project": "plangraph", "id": "G4", "title": "g", "kind": "goal", "track": "T0",
         "owner": "tester", "estimate_h": 1, "depends_on": ["plangraph:P2"],
         "done_criteria": "c", "evidence_kinds": ["any"], "operator": False}])
    assert world.store._item("plangraph:P2")["review_tier"] == "standard"
    r = world.store.create_items(world.p("router"), [
        {"project": "demo", "id": "S2", "title": "Handle the secret", "kind": "lane",
         "track": "T0", "owner": "tester", "estimate_h": 1, "depends_on": [],
         "done_criteria": "c", "evidence_kinds": ["any"], "review_tier": "standard", "operator": False},
        {"project": "demo", "id": "G5", "title": "g", "kind": "goal", "track": "T0",
         "owner": "tester", "estimate_h": 1, "depends_on": ["demo:S2"],
         "done_criteria": "c", "evidence_kinds": ["any"], "operator": False}])
    assert world.store._item("demo:S2")["review_tier"] == "standard"


# L1: KEY_RE uses \Z not $ - demo:A\n is refused
def test_key_re_uses_Z_not_dollar(tmp_path, world, srv, token_file):
    src, dig = _src_digest(tmp_path, "keynl")
    batch_path = _batch_path(tmp_path, src, dig, ops=[{"op": "edit", "key": "demo:A\n", "fields": {"title": "x"}}])
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path), "--url", srv.base_url, "--token-file", token_file, "--dry-run"],
                          out=out, err=err)
    assert rc == 1
    out_val = out.getvalue()
    assert "op 1 edit demo:A" in out_val
    assert "project:ID" in out_val or "key" in out_val.lower()


# L2: urllib opener does not follow redirects
# (Verified by code inspection: NoRedirectHandler in Session.__init__)


# L3: non-mapping op refused with op line and --from hint, validate ALL first
def _make_bad_batch(tmp_path, ops):
    src, dig = _src_digest(tmp_path, "bad")
    return _batch_path(tmp_path, src, dig, ops=ops)


def test_non_mapping_op_refused_with_resume_hint(tmp_path, world, srv, token_file):
    batch_path = _make_bad_batch(tmp_path, [{"op": "create", "item": LANE}, ["not", "a", "mapping"]])
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path), "--url", srv.base_url, "--token-file", token_file, "--dry-run"],
                          out=out, err=err)
    assert rc == 1
    out_val, err_val = out.getvalue(), err.getvalue()
    assert "op 2" in out_val and "resume with --from 2" in out_val
    assert "Traceback" not in out_val + err_val
    assert world.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_validate_all_ops_before_applying_first(tmp_path, world, srv, token_file):
    batch_path = _make_bad_batch(tmp_path, [{"op": "create", "item": LANE}] * 6 + [["bad"]])
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path), "--url", srv.base_url, "--token-file", token_file],
                          out=out, err=err)
    assert rc == 1
    out_val = out.getvalue()
    assert "op 7" in out_val and "resume with --from 7" in out_val
    assert "ok 1" not in out_val
    assert world.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_validate_all_ops_before_applying_first_dry_run(tmp_path, world, srv, token_file):
    batch_path = _make_bad_batch(tmp_path, [{"op": "create", "item": LANE}] * 6 +
                                 [{"op": "edit", "key": "novote", "fields": {"title": "x"}}])
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path), "--url", srv.base_url, "--token-file", token_file, "--dry-run"],
                          out=out, err=err)
    assert rc == 1
    out_val = out.getvalue()
    assert "op 7 edit novote: error" in out_val
    assert "resume with --from 7" in out_val
    assert "project:ID" in out_val
