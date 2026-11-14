"""The converter CLI runs real offline keeper rules on generic plan inputs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def run_convert(plan, *args):
    env = {k: v for k, v in os.environ.items()
           if k not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}
           and not k.startswith("GIT_CONFIG")}
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([sys.executable, "-m", "plangraph.keeper.cli", "plan",
                           "convert", str(plan), *map(str, args)],
                          capture_output=True, text=True, env=env)


@pytest.fixture
def plan(tmp_path):
    cards = {"seq": 10, "items": [
        {"project": "demo", "id": "GOAL", "key": "demo:GOAL", "kind": "goal",
         "state": "open", "title": "Goal", "depends_on": ["demo:OLD"],
         "done_criteria": "Result is measured by the report.", "review_tier": "standard"},
        {"project": "demo", "id": "OLD", "key": "demo:OLD", "kind": "lane",
         "state": "open", "title": "Old", "depends_on": [], "note": "Before",
         "done_criteria": "Old check", "review_tier": "standard"},
    ]}
    (tmp_path / "cards.json").write_text(json.dumps(cards))
    (tmp_path / "items.yaml").write_text(yaml.safe_dump({"items": [
        {"id": "NEW", "title": "New", "kind": "lane", "under": "GOAL",
         "done_criteria": "A local test checks the result.", "est_h": 1,
         "review_tier": "standard", "depends_on": []}]}))
    (tmp_path / "changes.yaml").write_text(yaml.safe_dump({
        "rewrite_dc": [{"id": "OLD", "done_criteria": "A test checks the new result.",
                        "title": "Renamed", "note": "More exact check"}],
        "reedge": [{"id": "OLD", "add": ["NEW"], "drop": []}],
        "reprio": [{"id": "OLD", "priority": 8}]}))
    return tmp_path


def invoke(plan):
    return run_convert(plan, "--project", "demo", "--base-seq", "10",
                       "--cards", plan / "cards.json", "--output", plan / "generated")


def test_cli_one_atomic_batch_and_manifest(plan):
    result = invoke(plan)
    assert result.returncode == 0, result.stderr
    batch = yaml.safe_load((plan / "generated/batch.yaml").read_text())
    assert [op["op"] for op in batch["ops"]] == ["create", "edit", "edit", "priority"]
    assert len(batch["ops"][0]["item"]) == 1
    fields = next(op["fields"] for op in batch["ops"] if op.get("key") == "demo:OLD")
    assert fields["title"] == "Renamed"
    assert fields["depends_on"] == ["demo:NEW"]
    assert fields["note"].startswith("Before\n")
    assert batch["base_seq"] == 10
    manifest = json.loads((plan / "generated/manifest.json").read_text())
    assert {Path(row["path"]).name for row in manifest["inputs"]} == {
        "cards.json", "items.yaml", "changes.yaml"}
    assert len(manifest["ordered_ops_sha256"]) == 64


def test_keeper_rejects_oversize_criteria(plan):
    data = yaml.safe_load((plan / "items.yaml").read_text())
    data["items"][0]["done_criteria"] = "x" * 601
    (plan / "items.yaml").write_text(yaml.safe_dump(data))
    result = invoke(plan)
    assert result.returncode != 0
    assert "validation" in result.stderr and "600" in result.stderr
    assert not (plan / "generated/batch.yaml").exists()


def update_yaml(plan, filename, change):
    path = plan / filename
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data))


@pytest.mark.parametrize("mode, code, detail", [
    ("credential", "validation", "credential"),
    ("deps", "validation", "200"),
    ("seat", "validation", "high"),
    ("state", "bad_state", "claimed"),
    ("stale", "conflict", "changed after base_seq"),
    ("cancelled", "depends_on_cancelled", "cancelled"),
])
def test_real_keeper_stop_codes(plan, mode, code, detail):
    cards = json.loads((plan / "cards.json").read_text())
    if mode == "credential":
        update_yaml(plan, "items.yaml", lambda d: d["items"][0].update(
            done_criteria="login example/placeholder1 is refused"))
    elif mode == "deps":
        update_yaml(plan, "items.yaml", lambda d: d["items"][0].update(
            depends_on=[f"D{i}" for i in range(201)]))
    elif mode == "seat":
        update_yaml(plan, "items.yaml", lambda d: d["items"][0].update(
            done_criteria="Two independent families ACCEPT the change."))
    elif mode == "state":
        cards["items"][1]["state"] = "claimed"
    elif mode == "stale":
        cards.update(seq=11, item_seqs={"demo:OLD": 11, "demo:GOAL": 10})
    elif mode == "cancelled":
        cards["items"].append(dict(cards["items"][1], id="CANCELLED",
                                  key="demo:CANCELLED", state="cancelled"))
        update_yaml(plan, "changes.yaml", lambda d: d["reedge"][0].update(add=["CANCELLED"]))
    (plan / "cards.json").write_text(json.dumps(cards))
    before = (plan / "cards.json").read_bytes()
    result = invoke(plan)
    assert result.returncode != 0, result.stdout
    assert code in result.stderr and detail in result.stderr, result.stderr
    assert not (plan / "generated").exists()
    assert (plan / "cards.json").read_bytes() == before


def test_coalesces_edits_and_orders_cancels_before_priorities(plan):
    cards = json.loads((plan / "cards.json").read_text())
    cards["items"].append(dict(cards["items"][1], id="RETIRE", key="demo:RETIRE"))
    (plan / "cards.json").write_text(json.dumps(cards))
    update_yaml(plan, "changes.yaml", lambda d: d.update(
        reowner=[{"id": "OLD", "owner": "builder"}],
        cancel=[{"id": "RETIRE", "reason": "Retired by replacement"}]))
    result = invoke(plan)
    assert result.returncode == 0, result.stderr
    ops = yaml.safe_load((plan / "generated/batch.yaml").read_text())["ops"]
    assert [op["op"] for op in ops] == ["create", "edit", "edit", "cancel", "priority"]
    assert len([op for op in ops if op["op"] == "edit" and op["key"] == "demo:OLD"]) == 1


def test_manifest_hashes_actual_bytes_and_ordered_ops(plan):
    import hashlib
    result = invoke(plan)
    assert result.returncode == 0, result.stderr
    manifest_bytes = (plan / "generated/manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    for entry in manifest["inputs"]:
        assert entry["sha256"] == hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest()
    batch = yaml.safe_load((plan / "generated/batch.yaml").read_text())
    assert batch["source_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    ops_bytes = json.dumps(batch["ops"], sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode()
    assert manifest["ordered_ops_sha256"] == hashlib.sha256(ops_bytes).hexdigest()
    result = invoke(plan)
    assert result.returncode != 0 and "output_exists" in result.stderr
    assert (plan / "generated/manifest.json").read_bytes() == manifest_bytes


@pytest.mark.parametrize("count, passes", [(500, True), (501, False)])
def test_atomic_op_limit_boundary(plan, count, passes):
    cards = json.loads((plan / "cards.json").read_text())
    cards["items"] = [dict(cards["items"][1], id=f"I{i}", key=f"demo:I{i}")
                      for i in range(count)]
    cards["items"].append({"project": "demo", "id": "GOAL", "key": "demo:GOAL",
                           "kind": "goal", "state": "open", "depends_on": []})
    (plan / "cards.json").write_text(json.dumps(cards))
    (plan / "items.yaml").write_text("items: []\n")
    (plan / "changes.yaml").write_text(yaml.safe_dump({
        "reprio": [{"id": f"I{i}", "priority": 7} for i in range(count)]}))
    result = invoke(plan)
    assert (result.returncode == 0) is passes, result.stderr
    if not passes:
        assert "validation" in result.stderr and "500" in result.stderr
        assert not (plan / "generated").exists()


def _reconstructed_cards(data_root, stop_batch, base_seq):
    """Historical YAML replay for fixture coverage; not an authoritative snapshot."""
    items = {}
    def q(project, value):
        return value if ":" in value else f"{project}:{value}"
    def create(row):
        row = dict(row)
        key = q(row["project"], row["id"])
        row.update(key=key, state=row.get("state", "open"))
        row["depends_on"] = [q(row["project"], d) for d in row.get("depends_on") or []]
        items[key] = row
    for path in sorted((data_root / "data/plan-v3").glob("*.yaml")):
        for row in (yaml.safe_load(path.read_text()) or {}).get("items", []):
            create(row)
    for path in sorted((data_root / "data/updates").glob("*.yaml")):
        if path.name >= stop_batch:
            break
        batch = yaml.safe_load(path.read_text()) or {}
        for op in batch.get("ops", []):
            name, key = op.get("op"), op.get("key")
            if name == "create":
                for row in op["item"] if isinstance(op["item"], list) else [op["item"]]:
                    create(row)
            elif key in items:
                row = items[key]
                if name == "edit":
                    fields = dict(op["fields"])
                    if isinstance(fields.get("depends_on"), dict):
                        spec = fields.pop("depends_on")
                        edges = set(row.get("depends_on") or [])
                        edges -= {q(row["project"], d) for d in spec.get("remove", [])}
                        edges |= {q(row["project"], d) for d in spec.get("add", [])}
                        fields["depends_on"] = sorted(edges)
                    row.update(fields)
                elif name in ("cancel", "close_on_decision", "legacy_state"):
                    row["state"] = {"cancel": "cancelled", "close_on_decision": "done"}.get(name, op.get("state"))
                elif name == "priority":
                    row["priority"] = op["priority"]
                elif name in ("add_dep", "remove_dep"):
                    edges = set(row.get("depends_on") or [])
                    dep = q(row["project"], op["dep"])
                    if name == "add_dep":
                        edges.add(dep)
                    else:
                        edges.discard(dep)
                    row["depends_on"] = sorted(edges)
    return {"seq": base_seq, "provenance": "reconstructed historical YAML; no authoritative keeper snapshot",
            "items": list(items.values())}


@pytest.mark.skipif(not os.environ.get("PG_PLAN_CONVERT_FIXTURE_DIR"),
                    reason="private plan fixture is opt-in")
@pytest.mark.parametrize("name, stop, seq, files, cards_env, expected", [
    ("main", "2026-11-12-plan-improve.yaml", 18341, ["items.yaml", "changes.yaml"],
     "PG_PLAN_CONVERT_CARDS", "not_found:"),
    ("followup", "2026-11-13-plan-improve-followup.yaml", 18989,
     ["followup-lessons.yaml", "followup-audit.yaml"], "PG_PLAN_CONVERT_FOLLOWUP_CARDS",
     "criteria replacement must match exactly once"),
])
def test_private_plan_data_cli(name, stop, seq, files, cards_env, expected):
    import uuid
    directory = Path(os.environ["PG_PLAN_CONVERT_FIXTURE_DIR"]).resolve()
    data_root = directory.parents[2]
    artifact = data_root / ".w3-artifacts" / "plan-convert" / str(uuid.uuid4())
    artifact.mkdir(parents=True)
    supplied = os.environ.get(cards_env)
    cards = Path(supplied) if supplied else artifact / "reconstructed-cards.json"
    if not supplied:
        cards.write_text(json.dumps(_reconstructed_cards(data_root, stop, seq)))
    data_args = [part for filename in files for part in ("--data", filename)]
    result = run_convert(directory, "--cards", cards, "--base-seq", str(seq),
                         "--output", artifact / name, *data_args)
    (artifact / "outcome.json").write_text(json.dumps({
        "cards": str(cards), "authoritative": bool(supplied),
        "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}))
    if supplied:
        assert result.returncode == 0, f"Private integration artifacts: {artifact}\n{result.stderr}"
    else:
        assert result.returncode == 2 and expected in result.stderr, result.stderr
        assert not (artifact / name).exists()


def test_accepts_keeper_head_seq_and_defaults_base(plan):
    cards = json.loads((plan / "cards.json").read_text())
    cards["head_seq"] = cards.pop("seq")
    (plan / "cards.json").write_text(json.dumps(cards))
    result = run_convert(plan, "--project", "demo", "--cards", plan / "cards.json",
                         "--output", plan / "generated")
    assert result.returncode == 0, result.stderr
    assert yaml.safe_load((plan / "generated/batch.yaml").read_text())["base_seq"] == 10


def test_followup_schema_and_merge_provenance(plan):
    cards = json.loads((plan / "cards.json").read_text())
    cards["items"].append(dict(cards["items"][1], id="RETIRE", key="demo:RETIRE"))
    (plan / "cards.json").write_text(json.dumps(cards))
    update_yaml(plan, "changes.yaml", lambda d: d.update(
        edits=[{"id": "OLD", "criteria": "Exact followup criterion", "priority": 9,
                "depends_on": {"add": ["NEW"]}}],
        merge=[{"id": "RETIRE", "into": "OLD", "reason": "Same required capability"}],
        close=[{"id": "OLD", "basis": "Requires evidence"}]))
    result = invoke(plan)
    assert result.returncode == 0, result.stderr
    batch = yaml.safe_load((plan / "generated/batch.yaml").read_text())
    edit = next(op for op in batch["ops"] if op.get("key") == "demo:OLD" and op["op"] == "edit")
    assert edit["fields"]["done_criteria"] == "Exact followup criterion"
    assert "Absorbs demo:RETIRE" in edit["fields"]["note"]
    manifest = json.loads((plan / "generated/manifest.json").read_text())
    assert manifest["not_converted"][0]["section"] == "close"


@pytest.mark.parametrize("file, text", [("items.yaml", "items: []\nitems: []\n"),
                                        ("changes.yaml", "reedge: [{id: OLD, add: [42]}]\n")])
def test_malformed_data_refused_before_output(plan, file, text):
    (plan / file).write_text(text)
    result = invoke(plan)
    assert result.returncode != 0
    assert not (plan / "generated").exists()


def test_completed_cards_keep_keeper_locks(plan):
    cards = json.loads((plan / "cards.json").read_text())
    cards["items"][1]["state"] = "done"
    (plan / "cards.json").write_text(json.dumps(cards))
    (plan / "items.yaml").write_text("items: []\n")
    (plan / "changes.yaml").write_text(yaml.safe_dump({"reprio": [{"id": "OLD", "priority": 8}]}))
    result = invoke(plan)
    assert result.returncode == 2 and "locked" in result.stderr, result.stderr
