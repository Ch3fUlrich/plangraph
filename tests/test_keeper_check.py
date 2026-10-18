"""``pg check``: read-only plan hygiene (order-only edges, merged-but-open items)."""
import json
import re
import subprocess
from types import SimpleNamespace

import pytest

from keeper_util import World, defn, to_done
from plangraph.keeper import cli, cli_check


@pytest.fixture
def world(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def create(world, *definitions):
    """Create the definitions plus the goal the plan validation insists on."""
    defs = list(definitions)
    if not any(d.get("kind") == "goal" for d in defs):
        defs.append(defn("GOAL", [d["id"] for d in defs], kind="goal"))
    world.store.create_items(world.p("router"), defs)


def run_main(world, tmp_path, monkeypatch, argv=()):
    """``pg check`` through the real CLI, reading the local plan snapshot."""
    snapshot = tmp_path / "plan.snapshot.json"
    snapshot.write_text(
        json.dumps(world.store.snapshot(world.p("router"))), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PG_CONFIG", raising=False)
    return cli.main(["check", *argv])


class _Session:
    """What cmd_check reads from a session: snapshot items and the repos map."""

    def __init__(self, items, repos=None, json_mode=False):
        self._items = items
        self.config = SimpleNamespace(repos=repos or {})
        self.json_mode = json_mode

    def local_items(self):
        return self._items


def run_check(world, repos=None, strict=False, json_mode=False):
    session = _Session(world.store.snapshot(world.p("router"))["items"],
                       repos, json_mode)
    return cli_check.cmd_check(SimpleNamespace(strict=strict), session)


def _repo(tmp_path, keys):
    """A throwaway git repo whose commits carry ``Plan-Item: <key>`` trailers."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(("git", *args), cwd=str(repo), check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "tester")
    git("config", "user.email", "tester@example.invalid")
    shas = []
    for index, key in enumerate(keys):
        (repo / f"f{index}.txt").write_text(f"{key}\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-m", f"work\n\nPlan-Item: {key}")
        shas.append(git("rev-parse", "HEAD"))
    return repo, shas


def test_check_is_registered_in_the_cli():
    args = cli.build_parser().parse_args(["check", "--strict"])
    assert args.command == "check" and args.strict
    assert cli.HANDLERS["check"] is cli_check.cmd_check


def test_order_only_edge_is_reported(world, tmp_path, monkeypatch, capsys):
    create(world, defn("A", []), defn("B", ["A"]),
           defn("D", ["A"], paths=["docs/notes.md"]))
    assert run_main(world, tmp_path, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "ORDER-ONLY demo:B -> demo:A" in out
    assert "ORDER-ONLY demo:D -> demo:A" in out


def test_strict_exits_1_on_findings_but_plain_exits_0(world, tmp_path, monkeypatch, capsys):
    create(world, defn("A", []), defn("B", ["A"]))
    assert run_main(world, tmp_path, monkeypatch, ["--strict"]) == 1
    capsys.readouterr()
    assert run_main(world, tmp_path, monkeypatch) == 0


def test_json_mode_emits_findings(world, tmp_path, monkeypatch, capsys):
    create(world, defn("A", []), defn("B", ["A"]))
    assert run_main(world, tmp_path, monkeypatch, ["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {"src": "demo:B", "dst": "demo:A"} in data["order_only"]
    assert data["merged_open"] == []


def test_mention_or_shared_paths_is_not_order_only(world, capsys):
    create(world,
           defn("A", [], done_criteria="lands the parser demo:B adds",
                paths=["src/plangraph/keeper"]),
           defn("B", ["A"]),
           defn("C", ["A"], paths=["src/plangraph/keeper/cli_check.py"]))
    assert run_check(world) == 0
    out = capsys.readouterr().out
    assert "ORDER-ONLY demo:B -> demo:A" not in out
    assert "ORDER-ONLY demo:C -> demo:A" not in out


def test_clean_plan_reports_nothing(world, capsys):
    create(world, defn("A", [], done_criteria="lands with demo:GOAL"),
           defn("GOAL", ["A"], kind="goal"))
    assert run_check(world, strict=True) == 0
    out = capsys.readouterr().out
    assert "ORDER-ONLY" not in out and "MERGED-OPEN" not in out


def test_merged_but_open_item_is_reported(world, tmp_path, capsys):
    create(world,
           defn("A", [], done_criteria="lands with demo:GOAL"),
           defn("B", [], done_criteria="lands with demo:GOAL"),
           defn("GOAL", ["A", "B"], kind="goal"))
    to_done(world, "demo:A")
    repo, shas = _repo(tmp_path, ["demo:A", "demo:B"])
    assert run_check(world, {"demo": str(repo)}) == 0
    found = re.findall(r"^MERGED-OPEN (\S+) ([0-9a-f]{40})$",
                       capsys.readouterr().out, re.M)
    assert found == [("demo:B", shas[1])]


def test_strict_exits_1_on_merged_but_open(world, tmp_path, capsys):
    create(world, defn("B", [], done_criteria="lands with demo:GOAL"),
           defn("GOAL", ["B"], kind="goal"))
    repo, _ = _repo(tmp_path, ["demo:B"])
    assert run_check(world, {"demo": str(repo)}, strict=True) == 1
    out = capsys.readouterr().out
    assert "MERGED-OPEN demo:B" in out and "ORDER-ONLY" not in out


def test_repo_configured_by_dict_entry(world, tmp_path, capsys):
    create(world, defn("B", [], done_criteria="lands with demo:GOAL"),
           defn("GOAL", ["B"], kind="goal"))
    repo, shas = _repo(tmp_path, ["demo:B"])
    assert run_check(world, {"demo": {"path": str(repo), "ref": "main"}}) == 0
    assert f"MERGED-OPEN demo:B {shas[0]}" in capsys.readouterr().out


def test_missing_repo_is_not_a_finding(world, tmp_path, capsys):
    create(world, defn("B", [], done_criteria="lands with demo:GOAL"),
           defn("GOAL", ["B"], kind="goal"))
    assert run_check(world, {"demo": str(tmp_path / "nope")}, strict=True) == 0
    assert "MERGED-OPEN" not in capsys.readouterr().out
