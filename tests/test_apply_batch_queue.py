"""PG-LE-APPLY-QUEUE: apply_batch owns the QUEUE line and exit status."""
import io
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import apply_batch  # noqa: E402
from test_apply_batch import (  # noqa: E402,F401
    ops_for,
    run,
    srv,
    token_file,
    world,
    write_batch,
)


def qrun(batch_path, url, token_path, qpath, qtext, extra=()):
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main(
        [str(batch_path), "--url", url, "--token-file", token_path,
         "--queue-line", qtext, "--queue-file", str(qpath), *extra],
        out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def test_success_appends_exactly_once(world, srv, token_file, tmp_path):
    batch = write_batch(tmp_path, name="qok")
    qp = tmp_path / "q.txt"
    rc, out, err = qrun(batch, srv.base_url, token_file, qp, "LIVE qok")
    assert rc == 0, out + err
    assert "applied 6 of 6" in out
    assert out.rstrip().splitlines()[-1] == f"queue: appended to {qp}"
    lines = qp.read_text(encoding="utf-8").splitlines()
    assert lines == ["LIVE qok"]
    assert stat.S_IMODE(os.stat(qp).st_mode) == 0o600


def test_two_successes_append_two_lines(world, srv, token_file, tmp_path):
    batch = write_batch(tmp_path, name="q2")
    qp = tmp_path / "q2.txt"
    rc, out, err = qrun(batch, srv.base_url, token_file, qp, "A1")
    assert rc == 0, out + err
    item_c = {"project": "demo", "id": "C", "title": "c lane",
              "kind": "lane", "track": "T0", "owner": "tester",
              "estimate_h": 1, "depends_on": [],
              "done_criteria": "criteria", "evidence_kinds": ["decision"],
              "review_tier": "none", "operator": False}
    item_gc = {"project": "demo", "id": "GC", "title": "goal c",
               "kind": "goal", "track": "T0", "owner": "tester",
               "estimate_h": 1, "depends_on": ["demo:C"],
               "done_criteria": "criteria", "evidence_kinds": ["any"],
               "operator": False}
    batch2 = write_batch(tmp_path, name="q2b",
                         ops=[{"op": "create", "item": [item_c, item_gc]}])
    rc, out, err = qrun(batch2, srv.base_url, token_file, qp, "A2")
    assert rc == 0, out + err
    assert qp.read_text(encoding="utf-8").splitlines() == ["A1", "A2"]


def test_keeper_refusal_writes_nothing_first_line_is_refusal(
        world, srv, token_file, tmp_path):
    ops = ops_for()
    ops[1] = {"op": "edit", "key": "demo:MISSING",
              "fields": {"title": "no such item"}}
    batch = write_batch(tmp_path, ops=ops, name="qref")
    qp = tmp_path / "qref.txt"
    rc, out, err = qrun(batch, srv.base_url, token_file, qp, "LIVE qref")
    assert rc == 1
    first = out.splitlines()[0] if out.splitlines() else ""
    assert first.startswith("op ") and "not_found" in first
    assert "acting as" in out
    assert out.splitlines()[0] != out.splitlines()[1]
    assert not qp.exists()


def test_principal_mismatch_writes_nothing(world, srv, tmp_path):
    other = tmp_path / "l1.token"
    other.write_text(world.tokens["l1main"], encoding="utf-8")
    batch = write_batch(tmp_path, name="qprinc")
    qp = tmp_path / "qprinc.txt"
    rc, out, err = qrun(batch, srv.base_url, str(other), qp, "LIVE x")
    assert rc == 1
    assert out.splitlines()[0].startswith("error: principal mismatch")
    assert not qp.exists()


def test_sha_mismatch_writes_nothing(world, srv, token_file, tmp_path):
    batch = write_batch(tmp_path, name="qsha", tamper=True)
    qp = tmp_path / "qsha.txt"
    rc, out, err = qrun(batch, srv.base_url, token_file, qp, "LIVE x")
    assert rc == 1
    assert out.splitlines()[0].startswith("source ")
    assert "sha256 mismatch" in out
    assert not qp.exists()


def test_dry_run_writes_nothing(world, srv, token_file, tmp_path):
    batch = write_batch(tmp_path, name="qdry")
    qp = tmp_path / "qdry.txt"
    rc, out, err = qrun(batch, srv.base_url, token_file, qp, "LIVE x",
                        extra=["--dry-run"])
    assert rc == 0
    assert not qp.exists()
    assert "queue: appended" not in out


def test_from_skip_writes_nothing(world, srv, token_file, tmp_path):
    from test_apply_batch import defn
    world.store.create_items(world.p("router"), [
        defn("B", [], evidence_kinds=["decision"], review_tier="none"),
        defn("GOAL", ["B"], kind="goal")])
    batch = write_batch(tmp_path, name="qfrom")
    qp = tmp_path / "qfrom.txt"
    rc, out, err = qrun(batch, srv.base_url, token_file, qp, "LIVE x",
                        extra=["--from", "2"])
    assert rc == 0
    assert "applied 5 of 6" in out
    assert not qp.exists()


def test_short_reply_writes_nothing(world, srv, token_file, tmp_path,
                                    monkeypatch):
    monkeypatch.setattr(apply_batch.Session, "post",
                        lambda self, verb, body: b'{"applied": 2}')
    batch = write_batch(tmp_path, name="qshort")
    qp = tmp_path / "qshort.txt"
    rc, out, err = qrun(batch, srv.base_url, token_file, qp, "LIVE x")
    assert rc == 1
    assert out.splitlines()[0].startswith("error: unreadable")
    assert not qp.exists()


def test_unreadable_reply_writes_nothing(world, srv, token_file, tmp_path,
                                         monkeypatch):
    monkeypatch.setattr(apply_batch.Session, "post",
                        lambda self, verb, body: b"<html>ok</html>")
    batch = write_batch(tmp_path, name="qbad")
    qp = tmp_path / "qbad.txt"
    rc, out, err = qrun(batch, srv.base_url, token_file, qp, "LIVE x")
    assert rc == 1
    assert out.splitlines()[0].startswith("error: unreadable")
    assert not qp.exists()


def test_only_one_flag_is_usage_error(world, srv, token_file, tmp_path):
    batch = write_batch(tmp_path, name="qone")
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(batch), "--url", srv.base_url,
                           "--token-file", token_file,
                           "--queue-line", "LIVE x"], out=out, err=err)
    assert rc == 2
    out2, err2 = io.StringIO(), io.StringIO()
    rc2 = apply_batch.main([str(batch), "--url", srv.base_url,
                            "--token-file", token_file,
                            "--queue-file", str(tmp_path / "q.txt")],
                           out=out2, err=err2)
    assert rc2 == 2


def test_newline_in_text_is_usage_error(world, srv, token_file, tmp_path):
    batch = write_batch(tmp_path, name="qnl")
    qp = tmp_path / "qnl.txt"
    for bad in ("a" + chr(10) + "b", "a" + chr(13) + "b"):
        out, err = io.StringIO(), io.StringIO()
        rc = apply_batch.main(
            [str(batch), "--url", srv.base_url, "--token-file", token_file,
             "--queue-line", bad, "--queue-file", str(qp)], out=out, err=err)
        assert rc == 2
    assert not qp.exists()


def test_append_failure_exits_3(world, srv, token_file, tmp_path):
    batch = write_batch(tmp_path, name="qfail")
    d = tmp_path / "rodir"
    d.mkdir()
    os.chmod(d, 0o500)
    qp = d / "q.txt"
    try:
        # make append fail: point queue file at a directory
        out, err = io.StringIO(), io.StringIO()
        rc = apply_batch.main(
            [str(batch), "--url", srv.base_url, "--token-file", token_file,
             "--queue-line", "LIVE x", "--queue-file", str(d)],
            out=out, err=err)
    finally:
        os.chmod(d, 0o700)
    assert rc == 3
    assert "batch applied but queue line not written" in (out.getvalue() + err.getvalue())
    assert out.getvalue().splitlines()[0].startswith("error: batch applied")
    assert "applied 6 of 6" in out.getvalue()
    assert world.conn.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 2


def test_mutation_guard_append_after_apply(world, srv, token_file, tmp_path,
                                          monkeypatch):
    calls = []
    real_open = os.open

    def spy_open(path, flags, *a, **k):
        if str(path).endswith("guard.txt"):
            calls.append(("open", len(world.conn.execute(
                "SELECT * FROM items").fetchall())))
        return real_open(path, flags, *a, **k)

    batch = write_batch(tmp_path, name="qguard")
    qp = tmp_path / "guard.txt"
    monkeypatch.setattr(os, "open", spy_open)
    rc, out, err = qrun(batch, srv.base_url, token_file, qp, "LIVE g")
    assert rc == 0
    assert calls and calls[0][1] == 2  # both items exist: apply ran first
    # ignoring rc: refusal must not append even if open is attempted
    monkeypatch.setattr(os, "open", spy_open)
    ops = ops_for()
    ops[1] = {"op": "edit", "key": "demo:MISSING",
              "fields": {"title": "x"}}
    batch2 = write_batch(tmp_path, ops=ops, name="qguard2")
    qp2 = tmp_path / "guard2.txt"
    rc2, out2, err2 = qrun(batch2, srv.base_url, token_file, qp2, "LIVE g2")
    assert rc2 == 1
    assert not qp2.exists()


def test_per_op_refusal_first_line_no_append(world, srv, token_file, tmp_path):
    ops = ops_for()
    ops[1] = {"op": "edit", "key": "demo:MISSING",
              "fields": {"title": "no such item"}}
    batch = write_batch(tmp_path, ops=ops, name="qperref")
    qp = tmp_path / "qperref.txt"
    rc, out, err = qrun(batch, srv.base_url, token_file, qp, "LIVE per",
                        extra=["--per-op"])
    assert rc == 1
    assert out.splitlines()[0].startswith("op 2 ") and "not_found" in out.splitlines()[0]
    assert not qp.exists()


def test_per_op_success_appends_once(world, srv, token_file, tmp_path):
    batch = write_batch(tmp_path, name="qperok")
    qp = tmp_path / "qperok.txt"
    rc, out, err = qrun(batch, srv.base_url, token_file, qp, "LIVE perok",
                        extra=["--per-op"])
    assert rc == 0, out + err
    assert "applied 6 of 6" in out
    assert qp.read_text(encoding="utf-8").splitlines() == ["LIVE perok"]


def test_whoami_failure_queue_first_line(world, srv, tmp_path):
    batch = write_batch(tmp_path, name="qwho")
    bad = tmp_path / "bad.token"
    bad.write_text("SECRETTOKEN0123456789abcdef", encoding="utf-8")
    qp = tmp_path / "qwho.txt"
    rc, out, err = qrun(batch, srv.base_url, str(bad), qp, "LIVE x")
    assert rc == 2
    assert out.splitlines()[0].startswith("error: whoami failed")
    assert "whoami failed" in err
    assert not qp.exists()


def test_from_out_of_range_queue_first_line(world, srv, token_file, tmp_path):
    batch = write_batch(tmp_path, name="qrange")
    qp = tmp_path / "qrange.txt"
    rc, out, err = qrun(batch, srv.base_url, token_file, qp, "LIVE x",
                        extra=["--from", "7"])
    assert rc == 2
    assert out.splitlines()[0].startswith("error: --from")
    assert "--from" in err
    assert not qp.exists()


def test_token_error_queue_first_line(world, srv, tmp_path):
    batch = write_batch(tmp_path, name="qtok")
    bad = tmp_path / "empty.token"
    bad.write_text("   ", encoding="utf-8")
    qp = tmp_path / "qtok.txt"
    rc, out, err = qrun(batch, srv.base_url, str(bad), qp, "LIVE x")
    assert rc == 2
    assert out.splitlines()[0].startswith("error:")
    assert not qp.exists()


def test_newline_queue_first_line(world, srv, token_file, tmp_path):
    batch = write_batch(tmp_path, name="qnl2")
    qp = tmp_path / "qnl2.txt"
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main(
        [str(batch), "--url", srv.base_url, "--token-file", token_file,
         "--queue-line", "a\nb", "--queue-file", str(qp)], out=out, err=err)
    assert rc == 2
    assert out.getvalue().splitlines()[0].startswith("error: --queue-line")
    assert not qp.exists()


def test_lone_surrogate_queue_rc3_no_file(world, srv, token_file, tmp_path):
    batch = write_batch(tmp_path, name="qsur")
    qp = tmp_path / "qsur.txt"
    rc, out, err = qrun(batch, srv.base_url, token_file, qp,
                        "x" + chr(0xDC80))
    assert rc == 3, out + err
    assert out.splitlines()[0].startswith(
        "error: batch applied but queue line not written")
    assert "applied 6 of 6" in out
    assert not qp.exists()


def test_symlink_queue_refused_target_unchanged(world, srv, token_file,
                                                tmp_path):
    batch = write_batch(tmp_path, name="qsym")
    target = tmp_path / "target.txt"
    target.write_text("ORIG\n", encoding="utf-8")
    qp = tmp_path / "qlink.txt"
    qp.symlink_to(target)
    rc, out, err = qrun(batch, srv.base_url, token_file, qp, "LIVE sym")
    assert rc == 3, out + err
    assert out.splitlines()[0].startswith(
        "error: batch applied but queue line not written")
    assert target.read_text(encoding="utf-8") == "ORIG\n"


def test_short_write_full_line_lands(world, srv, token_file, tmp_path,
                                     monkeypatch):
    real_write = os.write

    def one_byte(fd, data):
        return real_write(fd, bytes(data[:1]))

    monkeypatch.setattr(os, "write", one_byte)
    batch = write_batch(tmp_path, name="qshort2")
    qp = tmp_path / "qshort2.txt"
    rc, out, err = qrun(batch, srv.base_url, token_file, qp, "LIVE short")
    assert rc == 0, out + err
    assert qp.read_text(encoding="utf-8").splitlines() == ["LIVE short"]


def test_heuristic_spoof_not_promoted(world, srv, token_file, tmp_path,
                                      monkeypatch):
    import apply_batch_atomic as atomic
    real_warn = atomic.warnings
    monkeypatch.setattr(atomic, "warnings",
                        lambda reply: ["op 999 fake: should not win"])
    ops = ops_for()
    ops[1] = {"op": "edit", "key": "demo:MISSING",
              "fields": {"title": "no such item"}}
    batch = write_batch(tmp_path, ops=ops, name="qspoof")
    qp = tmp_path / "qspoof.txt"
    rc, out, err = qrun(batch, srv.base_url, token_file, qp, "LIVE spoof",
                        extra=["--per-op"])
    assert rc == 1
    assert "not_found" in out.splitlines()[0]
    assert "fake" not in out.splitlines()[0]
    assert not qp.exists()


def test_malformed_batch_queue_first_line_rc1(world, srv, token_file, tmp_path):
    bad = tmp_path / "bad-batch.yaml"
    bad.write_text("- not a mapping\n", encoding="utf-8")
    qp = tmp_path / "qbad.txt"
    rc, out, err = qrun(bad, srv.base_url, token_file, qp, "LIVE x")
    assert rc == 1
    assert out.splitlines()[0].startswith("error:")
    assert "error:" in err
    assert not qp.exists()


def test_missing_batch_file_queue_first_line_rc1(world, srv, token_file,
                                                 tmp_path):
    qp = tmp_path / "qmiss.txt"
    rc, out, err = qrun(tmp_path / "absent.yaml", srv.base_url, token_file,
                        qp, "LIVE x")
    assert rc == 1
    assert out.splitlines()[0].startswith("error:")
    assert not qp.exists()


def test_no_batch_argument_queue_first_line_rc2(world, srv, token_file,
                                                tmp_path):
    qp = tmp_path / "qnone.txt"
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main(
        ["--url", srv.base_url, "--token-file", token_file,
         "--queue-line", "LIVE x", "--queue-file", str(qp)], out=out, err=err)
    assert rc == 2
    assert out.getvalue().splitlines()[0].startswith("error:")
    assert not qp.exists()


def test_malformed_batch_without_queue_flags_keeps_stdout_empty(
        world, srv, token_file, tmp_path):
    bad = tmp_path / "bad2-batch.yaml"
    bad.write_text("- not a mapping\n", encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    rc = apply_batch.main([str(bad), "--url", srv.base_url,
                           "--token-file", token_file], out=out, err=err)
    assert rc == 1
    assert out.getvalue() == ""
    assert "error:" in err.getvalue()
