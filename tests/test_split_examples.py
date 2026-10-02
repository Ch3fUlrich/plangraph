"""examples/: two clearly fictional projects, a batch, and a rendered page stay valid."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools" / "split"))

import build_examples  # noqa: E402
import scrub_check as sc  # noqa: E402
from plangraph import views  # noqa: E402
from plangraph.keeper.importer import load_dir  # noqa: E402

EX = ROOT / "examples"


def test_two_fictional_projects_load():
    items = load_dir(EX / "plans")
    assert {i["project"] for i in items} == {"orchard", "lighthouse"}
    assert any(d.startswith("lighthouse:") for i in items for d in i["depends_on"])
    assert all("fictional" in (EX / "plans" / f"{p}.yaml").read_text()
               for p in ("orchard", "lighthouse"))


def test_snapshot_is_a_valid_graph_and_page_matches():
    snap = json.loads((EX / "snapshot.json").read_text())
    assert views.validate(snap["items"]) == []
    page = (EX / "rendered" / "demo.html").read_text()
    assert "orchard:O-3" in page and "<html" in page.lower()


def test_generated_files_are_up_to_date():
    assert build_examples.main(["--check"]) == 0


def test_batch_dry_runs_with_pinned_source(tmp_path):
    token = tmp_path / "tok"
    token.write_text("example-token\n")
    res = subprocess.run([sys.executable, str(ROOT / "tools" / "apply_batch.py"),
                          str(EX / "batch" / "demo-batch.yaml"), "--url", "http://127.0.0.1:1",
                          "--token-file", str(token), "--dry-run"],
                         capture_output=True, text=True, cwd=ROOT)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "dry run: cancel 1, create 1, priority 1" in res.stdout


def test_examples_pass_scrub_check():
    assert sc.scan_tree(EX, sc.build_config()) == []
