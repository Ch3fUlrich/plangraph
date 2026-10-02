"""X7d: tools/apply_batch.py — router batch applier over the keeper HTTP API.

A batch is an ordered ops YAML; the applier verifies ``source_sha256`` before
any call, replays each op as the token's principal, stops at the first refusal
with a ``--from`` resume hint, and never prints the token.
"""
import hashlib
import io
import json
import os
import subprocess
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

LANE_B = {"project": "demo", "id": "B", "title": "batched lane", "kind": "lane",
          "track": "T0", "owner": "tester", "estimate_h": 1, "depends_on": [],
          "done_criteria": "criteria", "evidence_kinds": ["decision"],
          "review_tier": "none", "operator": False}
GOAL = {"project": "demo", "id": "GOAL", "title": "goal", "kind": "goal",
        "track": "T0", "owner": "tester", "estimate_h": 1,
        "depends_on": ["demo:B"], "done_criteria": "criteria",
        "evidence_kinds": ["any"], "operator": False}


def ops_for():
    return [
        {"op": "create", "item": [dict(LANE_B), dict(GOAL)]},
        {"op": "edit", "key": "demo:B", "fields": {"title": "edited by batch"}},
        {"op": "legacy_state", "key": "demo:B", "state": "claimed",
         "note": "router batch"},
        {"op": "add_evidence", "key": "demo:B", "kind": "decision",
         "ref": {"id": "D-9"}},
        {"op": "close_on_decision", "key": "demo:B"},
        {"op": "cancel", "key": "demo:GOAL", "reason": "superseded"},
    ]


def write_batch(tmp_path, *, ops=None, name="batch", tamper=False):
    """Write an ops YAML plus its hashed source under the served batch root."""
    source = tmp_path / "plan" / f"{name}.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("recorded plan batch\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    doc = {"batch": f"X7d-{name}", "principal": "router",
           "source": str(source), "source_sha256": digest,
           "ops": ops if ops is not None else ops_for()}
    path = tmp_path / "plan" / f"{name}.yaml"
    text = yaml.safe_dump(doc, sort_keys=False)
    if tamper:
        doc["source_sha256"] = "f" * 64  # digest disagrees with the file
        text = yaml.safe_dump(doc, sort_keys=False)
    path.write_text(text, encoding="utf-8")
    return path


def state_of(w, key):
    return w.conn.execute("SELECT state FROM items WHERE key=?",
                          (key,)).fetchone()[0]


def title_of(w, key):
    return w.conn.execute("SELECT title FROM items WHERE key=?",
                          (key,)).fetchone()[0]


def item_count(w):
    return w.conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    decisions = tmp_path / "DECISIONS.md"
    decisions.write_text(
        "| ID | Note | Scope |\n|---|---|---|\n"
        "| D-9 | batch close | Closes: demo:B |\n", encoding="utf-8")
    w.store.ctx = SimpleNamespace(decisions=DecisionsAdapter(decisions))
    w.store.batch_roots = [str(tmp_path / "plan")]
    yield w
    w.close()


def make_cfg(tmp_path, world):
    path = tmp_path / "keeper.json"
    path.write_text(json.dumps({
        "db": str(world.path),
        "snapshot": str(tmp_path / "plan.snapshot.json"),
        "bind": "127.0.0.1:0",
    }), encoding="utf-8")
    os.chmod(path, 0o600)
    return config.load_config(path)


@pytest.fixture
def srv(world, tmp_path):
    server = api.make_server(world.store, make_cfg(tmp_path, world), port=0)
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


def run(batch_path, url, token_path, extra=()):
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path), "--url", url,
                           "--token-file", token_path, *extra],
                          out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def test_full_batch_applies_in_order(world, srv, token_file, tmp_path):
    batch_path = write_batch(tmp_path)
    rc, out, errtxt = run(batch_path, srv.base_url, token_file)
    assert rc == 0, out + errtxt
    for n in range(1, 7):
        assert f"ok {n} " in out
    assert "summary: add_evidence 1, cancel 1, close_on_decision 1, " \
        "create 1, edit 1, legacy_state 1" in out
    assert state_of(world, "demo:B") == "done"
    assert state_of(world, "demo:GOAL") == "cancelled"
    assert title_of(world, "demo:B") == "edited by batch"


def test_sha_mismatch_refuses_before_any_call(world, srv, token_file,
                                              tmp_path):
    batch_path = write_batch(tmp_path, tamper=True)
    rc, out, errtxt = run(batch_path, srv.base_url, token_file)
    assert rc == 1
    assert "sha256 mismatch" in out
    assert item_count(world) == 0


def test_refusal_mid_batch_reports_resume_from(world, srv, token_file,
                                               tmp_path):
    ops = ops_for()
    ops[1] = {"op": "edit", "key": "demo:MISSING",
              "fields": {"title": "no such item"}}
    batch_path = write_batch(tmp_path, ops=ops, name="midbatch")
    rc, out, errtxt = run(batch_path, srv.base_url, token_file)
    assert rc == 1
    assert "ok 1 create demo:B, demo:GOAL" in out
    assert "op 2 edit demo:MISSING: not_found" in out
    assert "resume with --from 2" in out
    assert state_of(world, "demo:B") == "open"

    rc2, out2, err2 = run(batch_path, srv.base_url, token_file,
                          extra=["--from", "3"])
    assert rc2 == 0, out2 + err2
    assert "op 2 " not in out2 and "ok 2 " not in out2
    assert "ok 3 legacy_state demo:B" in out2
    assert state_of(world, "demo:B") == "done"


def test_dry_run_makes_no_calls_and_checks_shape(world, srv, token_file,
                                                 tmp_path):
    batch_path = write_batch(tmp_path, name="dry")
    rc, out, errtxt = run(batch_path, srv.base_url, token_file,
                          extra=["--dry-run"])
    assert rc == 0, out + errtxt
    assert "plan 5 close_on_decision demo:B" in out
    assert "plan 6 cancel demo:GOAL" in out
    assert "dry run:" in out
    assert item_count(world) == 0

    bad = write_batch(tmp_path, name="bad",
                      ops=[{"op": "edit", "key": "novote",
                            "fields": {"title": "x"}}])
    rc2, out2, _ = run(bad, srv.base_url, token_file, extra=["--dry-run"])
    assert rc2 == 1
    assert "project:ID" in out2 + _
    assert item_count(world) == 0


def test_from_skips_earlier_ops(world, srv, token_file, tmp_path):
    world.store.create_items(world.p("router"), [
        defn("B", [], evidence_kinds=["decision"], review_tier="none"),
        defn("GOAL", ["B"], kind="goal")])
    rc, out, errtxt = run(write_batch(tmp_path, name="resumed"),
                          srv.base_url, token_file, extra=["--from", "2"])
    assert rc == 0, out + errtxt
    assert "op 1 " not in out and "ok 1 " not in out
    assert "ok 2 edit demo:B" in out
    assert state_of(world, "demo:B") == "done"


def test_token_never_appears_in_output(world, srv, tmp_path, token_file):
    token = world.tokens["router"]
    assert len(token) >= 16
    batch_path = write_batch(tmp_path, name="leak")
    rc, out, errtxt = run(batch_path, srv.base_url, token_file)
    assert rc == 0
    assert token not in out and token not in errtxt

    bad = tmp_path / "bad.token"
    secret = "SECRETTOKEN0123456789abcdef"
    bad.write_text(secret, encoding="utf-8")
    rc2, out2, err2 = run(batch_path, srv.base_url, str(bad))
    assert rc2 == 2  # whoami failure now exits 2 per M2
    assert "whoami failed" in err2;assert "ok " not in out2
    assert secret not in out2 + err2


def test_cli_subprocess_reports_401_without_leaking(world, srv, tmp_path):
    batch_path = write_batch(tmp_path, name="subproc")
    bad = tmp_path / "bad.token"
    secret = "SECRETTOKEN0123456789abcdef"
    bad.write_text(secret, encoding="utf-8")
    env = {**os.environ,
           "PYTHONPATH": os.pathsep.join([str(ROOT / "src"),
                                          str(ROOT / "tests")])}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "apply_batch.py"),
         str(batch_path), "--url", srv.base_url, "--token-file", str(bad)],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 2  # whoami failure now exits 2 per M2
    assert "whoami failed" in proc.stderr;assert "op 1" not in proc.stdout
    assert secret not in proc.stdout + proc.stderr


def test_from_out_of_range_refused_without_calls(world, srv, token_file,
                                                 tmp_path):
    batch_path = write_batch(tmp_path, name="range")
    for flag in ("0", "-1", "7"):
        rc, out, errtxt = run(batch_path, srv.base_url, token_file,
                              extra=["--from", flag])
        assert rc == 2, (flag, out, errtxt)
        assert "--from" in errtxt
        assert "ok " not in out
    assert item_count(world) == 0


def test_dry_run_sha_mismatch_refused(world, srv, token_file, tmp_path):
    batch_path = write_batch(tmp_path, name="drytamper", tamper=True)
    rc, out, errtxt = run(batch_path, srv.base_url, token_file,
                          extra=["--dry-run"])
    assert rc == 1
    assert "sha256 mismatch" in out
    assert item_count(world) == 0


def test_relative_source_resolved_against_batch_dir(world, srv, token_file,
                                                    tmp_path, monkeypatch):
    plan = tmp_path / "plan"
    plan.mkdir(parents=True, exist_ok=True)
    src = plan / "rel.md"
    src.write_text("recorded plan batch\n", encoding="utf-8")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    doc = {"batch": "X7d-rel", "principal": "router", "source": "rel.md",
           "source_sha256": digest, "ops": ops_for()}
    batch_path = plan / "rel.yaml"
    batch_path.write_text(yaml.safe_dump(doc, sort_keys=False),
                          encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    calls = []
    orig_post = apply_batch.Session.post
    def spy(self, verb, body):
        calls.append((verb, body))
        return orig_post(self, verb, body)
    apply_batch.Session.post = spy
    try:
        rc, out, errtxt = run(batch_path, srv.base_url, token_file)
    finally:
        apply_batch.Session.post = orig_post
    assert rc == 0, out + errtxt
    assert "applied 6 of 6" in out
    close = [body for verb, body in calls if verb == "close_on_decision"]
    assert len(close) == 1
    expect = f"{os.path.realpath(src)}@{digest}"
    assert close[0]["batch_ref"] == expect
    assert os.path.isabs(close[0]["batch_ref"].split("@")[0])


def test_principal_mismatch_refused_before_any_call(world, srv, tmp_path):
    other = tmp_path / "l1.token"
    other.write_text(world.tokens["l1main"], encoding="utf-8")
    rc, out, errtxt = run(write_batch(tmp_path, name="princ"),
                          srv.base_url, str(other))
    assert rc == 1
    assert "acting as l1main (l1)" in out
    assert "principal mismatch" in out
    assert "ok " not in out
    assert item_count(world) == 0


def test_unknown_keys_refused(world, srv, token_file, tmp_path):
    ops = [{"op": "edit", "key": "demo:B", "fields": {"title": "x"},
            "bogus": 1}]
    rc, out, errtxt = run(write_batch(tmp_path, name="unknownop", ops=ops),
                          srv.base_url, token_file)
    assert rc == 1
    assert "op 1 edit demo:B: error" in out
    assert "unknown field" in out
    assert "resume with --from 1" in out
    assert item_count(world) == 0
    doc = {"batch": "X7d-top", "principal": "router",
           "source": "x.md", "source_sha256": "f" * 64,
           "ops": ops_for(), "bogus_top": 1}
    plan = tmp_path / "plan"
    top = plan / "top.yaml"
    top.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rc2, out2, err2 = run(top, srv.base_url, token_file)
    assert rc2 == 1
    assert "unknown top-level key" in out2 + err2


def test_duplicate_key_refused(world, srv, token_file, tmp_path):
    plan = tmp_path / "plan"
    plan.mkdir(parents=True, exist_ok=True)
    src = plan / "dup.md"
    src.write_text("recorded plan batch\n", encoding="utf-8")
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    text = (f"batch: X7d-dup\nprincipal: router\nsource: {src}\n"
            f"source_sha256: {digest}\nops:\n  - op: edit\n"
            f"    key: demo:B\n    key: demo:C\n"
            f"    fields: {{title: x}}\n")
    dup = plan / "dup.yaml"
    dup.write_text(text, encoding="utf-8")
    rc, out, errtxt = run(dup, srv.base_url, token_file)
    assert rc == 1
    assert "duplicate key" in out + errtxt
    assert item_count(world) == 0


def test_missing_token_file_exits_2(world, srv, tmp_path):
    batch_path = write_batch(tmp_path, name="notoken")
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch_path), "--url", srv.base_url],
                          out=out, err=err)
    assert rc == 2
    assert item_count(world) == 0


def test_dry_run_bad_op_uses_resume_format(world, srv, token_file, tmp_path):
    bad = write_batch(tmp_path, name="drybad",
                      ops=[{"op": "edit", "key": "novote",
                            "fields": {"title": "x"}}])
    rc, out, errtxt = run(bad, srv.base_url, token_file,
                          extra=["--dry-run"])
    assert rc == 1
    assert "op 1 edit novote: error" in out
    assert "resume with --from 1" in out
    assert item_count(world) == 0


def test_summary_reports_applied_and_skipped(world, srv, token_file,
                                             tmp_path):
    world.store.create_items(world.p("router"), [
        defn("B", [], evidence_kinds=["decision"], review_tier="none"),
        defn("GOAL", ["B"], kind="goal")])
    rc, out, errtxt = run(write_batch(tmp_path, name="summ"),
                          srv.base_url, token_file, extra=["--from", "2"])
    assert rc == 0, out + errtxt
    assert "applied 5 of 6" in out
    assert "skipped 1 (before --from)" in out
def test_whoami_401_fails_closed(world,srv,tmp_path):
    bad=tmp_path/"w401.token";sec="SECRETTOKEN0123456789abcdef";bad.write_text(sec);rc,out,err=run(write_batch(tmp_path,name="w401"),srv.base_url,str(bad));assert rc==2;assert "op 1" not in out;assert "whoami" in err.lower();assert item_count(world)==0;assert sec not in out+err
def test_ref_type_refused(world,srv,token_file,tmp_path):
    for ref in ("s",7):
        ops=[{"op":"add_evidence","key":"demo:B","kind":"decision","ref":ref}]
        for x in ([],["--dry-run"]):
            rc,out,err=run(write_batch(tmp_path,ops=ops),srv.base_url,token_file,extra=x);assert rc==1,(ref,x,out+err);assert "op 1 add_evidence demo:B: error" in out;assert "resume with --from 1" in out
    assert item_count(world)==0
def test_unhashable_key(world,srv,token_file,tmp_path):
    p=tmp_path/"plan";p.mkdir(parents=True,exist_ok=True);s=p/"uh.md";s.write_text("x\n");d=hashlib.sha256(s.read_bytes()).hexdigest();(p/"uh.yaml").write_text(f"batch: X\nprincipal: router\nsource: {s}\nsource_sha256: {d}\nops:\n- op: edit\n  key: demo:B\n  fields:\n    ? [1, 2]\n    : x\n")
    rc,out,err=run(p/"uh.yaml",srv.base_url,token_file);assert rc==1;assert "invalid mapping key" in out+err;assert "Traceback" not in out+err;assert item_count(world)==0
def test_date_field_refused(world,srv,token_file,tmp_path):
    p=tmp_path/"plan";p.mkdir(parents=True,exist_ok=True);s=p/"dt.md";s.write_text("x\n");d=hashlib.sha256(s.read_bytes()).hexdigest();(p/"dt.yaml").write_text(f"batch: X\nprincipal: router\nsource: {s}\nsource_sha256: {d}\nops:\n- {{op: edit, key: demo:B, fields: {{due: 2026-10-02}}}}\n")
    rc,out,err=run(p/"dt.yaml",srv.base_url,token_file);assert rc==1;assert "op 1 edit demo:B: error" in out;assert "resume with --from 1" in out;assert "Traceback" not in out+err
def test_symlink_batch_resolves_beside_real(world,srv,token_file,tmp_path):
    r=tmp_path/"plan"/"real";r.mkdir(parents=True,exist_ok=True);(r/"s.md").write_text("recorded plan batch\n");d=hashlib.sha256((r/"s.md").read_bytes()).hexdigest();(r/"b.yaml").write_text(yaml.safe_dump({"batch":"X","principal":"router","source":"s.md","source_sha256":d,"ops":ops_for()}));L=tmp_path/"link.yaml";L.exists() or L.symlink_to(r/"b.yaml")
    rc,out,err=run(str(L),srv.base_url,token_file);assert rc==0,out+err;assert "applied 6 of 6" in out
