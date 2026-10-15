"""Contract 8: batch files applied through ``tools/apply_batch.py`` (CLI) and ``apply_ops``."""
import pytest

from contract_batch import goal, lane, run_batch, write_batch
from contract_world import err, need, ok, show, uid


def _exists(kp, key):
    return kp.get(f"/v1/show?key={key}&fmt=json", "reader").status == 200


def _plain_ops(tag):
    return [
        {"op": "create", "item": [lane("demo", f"B1-{tag}"), lane("demo", f"B2-{tag}", [f"B1-{tag}"]),
                                  goal("demo", f"BG-{tag}", [f"B2-{tag}"])]},
        {"op": "edit", "key": f"demo:B2-{tag}", "fields": {"title": "edited by batch"}},
        {"op": "priority", "key": f"demo:B1-{tag}", "priority": 7},
        {"op": "cancel", "key": f"demo:B2-{tag}", "reason": "superseded"},
    ]


def test_happy_path_applies_atomically_and_reports_each_op(kp, tmp_path):
    tag = uid()
    ops = _plain_ops(tag)[:3]
    out = run_batch(kp, write_batch(tmp_path, ops))
    assert out.returncode == 0, out.stdout + out.stderr
    assert "acting as router (router)" in out.stdout
    for n, name in ((1, "create"), (2, "edit"), (3, "priority")):
        assert f"ok {n} {name}" in out.stdout
    assert "summary:" in out.stdout and "applied 3 of 3" in out.stdout
    assert kp.tokens["router"] not in out.stdout + out.stderr
    assert show(kp, f"demo:B2-{tag}")["title"] == "edited by batch"
    assert show(kp, f"demo:B1-{tag}")["priority"] == 7
    assert show(kp, f"demo:B2-{tag}")["depends_on"] == [{"key": f"demo:B1-{tag}", "state": "open"}]


def test_later_ops_see_earlier_ops_and_final_graph_is_validated(kp, tmp_path):
    tag = uid()
    out = run_batch(kp, write_batch(tmp_path, _plain_ops(tag)[:1] + [
        {"op": "cancel", "key": f"demo:B1-{tag}", "reason": "same batch"}]))
    assert out.returncode == 1 and "depends_on_cancelled" in out.stdout   # goal would hang on a cancelled lane
    assert "nothing applied" in out.stdout
    assert not _exists(kp, f"demo:B1-{tag}")


def test_dry_run_makes_no_calls(kp, tmp_path):
    tag = uid()
    out = run_batch(kp, write_batch(tmp_path, _plain_ops(tag)[:2]), args=["--dry-run"])
    assert out.returncode == 0, out.stdout + out.stderr
    assert "plan 1 create" in out.stdout and "no calls made" in out.stdout
    assert not _exists(kp, f"demo:B1-{tag}")


def test_refusal_rolls_back_the_whole_batch(kp, tmp_path):
    tag = uid()
    ops = _plain_ops(tag)[:1] + [{"op": "edit", "key": f"demo:NOPE-{tag}", "fields": {"title": "x"}}]
    out = run_batch(kp, write_batch(tmp_path, ops))
    assert out.returncode == 1
    assert f"op 2 edit demo:NOPE-{tag}: not_found" in out.stdout
    assert "nothing applied (the whole batch was rolled back)" in out.stdout
    assert not _exists(kp, f"demo:B1-{tag}") and not _exists(kp, f"demo:BG-{tag}")


def test_whole_graph_problems_refuse_the_batch(kp, tmp_path):
    tag = uid()
    cyc = [{"op": "create", "item": [lane("demo", f"C1-{tag}", [f"C2-{tag}"]),
                                     lane("demo", f"C2-{tag}", [f"C1-{tag}"]),
                                     goal("demo", f"CG-{tag}", [f"C1-{tag}"])]}]
    out = run_batch(kp, write_batch(tmp_path, cyc))
    assert out.returncode == 1 and "cycle" in out.stdout and "nothing applied" in out.stdout
    dangling = [{"op": "create", "item": [lane("demo", f"U1-{tag}", [f"MISSING-{tag}"]),
                                          goal("demo", f"UG-{tag}", [f"U1-{tag}"])]}]
    out = run_batch(kp, write_batch(tmp_path, dangling))
    assert out.returncode == 1 and "unknown_dep" in out.stdout
    assert not _exists(kp, f"demo:U1-{tag}")


def test_source_pin_mismatch_is_refused_before_any_call(kp, tmp_path):
    tag = uid()
    path = write_batch(tmp_path, _plain_ops(tag)[:1], digest="0" * 64)
    out = run_batch(kp, path)
    assert out.returncode == 1 and "sha256 mismatch" in out.stdout
    assert "acting as" not in out.stdout                      # no whoami: nothing was called
    assert not _exists(kp, f"demo:B1-{tag}")
    good = write_batch(tmp_path, _plain_ops(tag)[:1], name="edited", source_text="v1\n")
    (tmp_path / "edited.md").write_text("v2\n", encoding="utf-8")   # changed after pinning
    assert "sha256 mismatch" in run_batch(kp, good).stdout


def test_principal_must_match_the_token_and_be_a_router(kp, tmp_path):
    tag = uid()
    ops = _plain_ops(tag)[:1]
    out = run_batch(kp, write_batch(tmp_path, ops, principal="lead"), who="lead")
    assert out.returncode == 1 and "principal mismatch" in out.stdout
    out = run_batch(kp, write_batch(tmp_path, ops, principal="router"), who="lead")
    assert out.returncode == 1 and "principal mismatch" in out.stdout
    assert not _exists(kp, f"demo:B1-{tag}")


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["ops"].append({"op": "explode", "key": "demo:X"}), "unknown op"),
    (lambda d: d["ops"][0].update(bogus=1), "unknown field"),
    (lambda d: d.update(extra_key=1), "unknown top-level key"),
    (lambda d: d.pop("source_sha256"), "missing 'source_sha256'"),
    (lambda d: d.update(ops=[]), "non-empty list"),
])
def test_malformed_batch_files_are_refused_locally(kp, tmp_path, mutate, needle):
    import yaml
    tag = uid()
    path = write_batch(tmp_path, _plain_ops(tag)[:1])
    doc = yaml.safe_load(path.read_text())
    mutate(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    out = run_batch(kp, path)
    assert out.returncode != 0 and needle in out.stdout + out.stderr
    assert not _exists(kp, f"demo:B1-{tag}")


def test_duplicate_yaml_keys_are_refused(kp, tmp_path):
    path = write_batch(tmp_path, _plain_ops(uid())[:1])
    path.write_text(path.read_text().replace("ops:", "batch: again\nops:"), encoding="utf-8")
    out = run_batch(kp, path)
    assert out.returncode != 0 and "duplicate key" in out.stdout + out.stderr


def test_per_op_mode_stops_at_first_refusal_and_resumes(kp, tmp_path):
    tag = uid()
    ops = [{"op": "create", "item": [lane("demo", f"P1-{tag}"), goal("demo", f"PG-{tag}", [f"P1-{tag}"])]},
           {"op": "edit", "key": f"demo:NOPE-{tag}", "fields": {"title": "x"}},
           {"op": "priority", "key": f"demo:P1-{tag}", "priority": 4}]
    path = write_batch(tmp_path, ops)
    out = run_batch(kp, path, args=["--per-op"])
    assert out.returncode == 1 and "ok 1 create" in out.stdout
    assert "op 2 edit" in out.stdout and "resume with --from 2" in out.stdout
    assert _exists(kp, f"demo:P1-{tag}")                    # per-op keeps partial progress
    out = run_batch(kp, path, args=["--from", "3"])
    assert out.returncode == 0 and "applied 1 of 3, skipped 2 (before --from)" in out.stdout
    assert show(kp, f"demo:P1-{tag}")["priority"] == 4


def test_queue_line_is_appended_only_after_a_complete_apply(kp, tmp_path):
    tag = uid()
    queue = tmp_path / "queue.txt"
    good = write_batch(tmp_path, _plain_ops(tag)[:1])
    out = run_batch(kp, good, args=["--queue-line", "DONE one", "--queue-file", str(queue)])
    assert out.returncode == 0 and queue.read_text() == "DONE one\n"
    bad = write_batch(tmp_path, [{"op": "edit", "key": f"demo:NOPE-{tag}", "fields": {"title": "x"}}])
    out = run_batch(kp, bad, args=["--queue-line", "DONE two", "--queue-file", str(queue)])
    assert out.returncode == 1 and queue.read_text() == "DONE one\n"


def test_close_on_decision_batch_closes_a_tier_none_item(kp, tmp_path):
    need(kp, "decisions")
    need(kp, "batch")
    probe = kp.get("/v1/show?key=demo:CLOSE-1&fmt=json", "reader")
    if probe.status == 200:
        pytest.skip("fixture item demo:CLOSE-1 was already used on this keeper")
    ops = [{"op": "create", "item": [
                lane("demo", "CLOSE-1", review_tier="none"), goal("demo", "CLOSE-G", ["CLOSE-1"])]},
           {"op": "legacy_state", "key": "demo:CLOSE-1", "state": "claimed", "note": "router batch"},
           {"op": "add_evidence", "key": "demo:CLOSE-1", "kind": "decision", "ref": {"id": "D-2"}},
           {"op": "close_on_decision", "key": "demo:CLOSE-1"}]
    out = run_batch(kp, write_batch(kp.batch_dir, ops, name="close-" + uid().lower()))
    assert out.returncode == 0, out.stdout + out.stderr
    assert "applied 4 of 4" in out.stdout
    done = show(kp, "demo:CLOSE-1")
    assert done["state"] == "done" and done["reviewed"] is False
    err(kp.post("cancel", "router", {"key": "demo:CLOSE-1"}), 409, "locked")


def test_close_on_decision_outside_batch_roots_is_refused(kp, tmp_path):
    need(kp, "decisions")
    tag = uid()
    ops = [{"op": "create", "item": [lane("demo", f"NC-{tag}", review_tier="none"),
                                     goal("demo", f"NG-{tag}", [f"NC-{tag}"])]},
           {"op": "legacy_state", "key": f"demo:NC-{tag}", "state": "claimed"},
           {"op": "add_evidence", "key": f"demo:NC-{tag}", "kind": "decision", "ref": {"id": "D-1"}},
           {"op": "close_on_decision", "key": f"demo:NC-{tag}"}]
    out = run_batch(kp, write_batch(tmp_path, ops))      # tmp_path is not a batch root
    assert out.returncode == 1 and "forbidden" in out.stdout
    assert not _exists(kp, f"demo:NC-{tag}")


# --------------------------------------------------------------- apply_ops over HTTP
def test_apply_ops_http_shape_and_gate(kp):
    tag = uid()
    body = {"ops": [{"op": "create", "item": lane("demo", f"H1-{tag}")},
                    {"op": "create", "item": goal("demo", f"HG-{tag}", [f"H1-{tag}"])},
                    {"op": "priority", "key": f"demo:H1-{tag}", "priority": 3}]}
    for who in ("lead", "worker1", "reader", "reviewer1"):
        err(kp.post("apply_ops", who, body), 403, "forbidden")
    res = ok(kp.post("apply_ops", "router", body), "ApplyOps")
    assert res["applied"] == 3 and len(res["results"]) == 3
    assert show(kp, f"demo:H1-{tag}")["priority"] == 3


@pytest.mark.parametrize("ops,code", [
    ([], "validation"),
    ("nope", "validation"),
    ([{"op": "explode"}], "validation"),
    ([{"op": "edit", "key": "demo:X"}], "validation"),
    ([{"op": "priority", "key": "demo:X", "priority": True}], "validation"),
    ([{"op": "close_on_decision", "key": "demo:X"}], "validation"),    # no batch_ref
])
def test_apply_ops_http_shape_refusals(kp, ops, code):
    body = err(kp.post("apply_ops", "router", {"ops": ops}), 400, code)
    assert "msg" in body


def test_apply_ops_names_the_failing_op_and_rolls_back(kp):
    tag = uid()
    body = {"ops": [{"op": "create", "item": lane("demo", f"Z1-{tag}")},
                    {"op": "cancel", "key": f"demo:NOPE-{tag}"}]}
    resp = err(kp.post("apply_ops", "router", body), 404, "not_found")
    assert resp["msg"].startswith("op 2 cancel ")
    assert not _exists(kp, f"demo:Z1-{tag}")
