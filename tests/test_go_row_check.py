"""One GO-row parser: the keeper's. The fixture table is shared with every other reader of a GO row."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from plangraph.keeper import cli
from plangraph.keeper.store_runs import go_row_check

ROOT = Path(__file__).resolve().parent.parent
HEAD = "a" * 40
TABLE = json.loads((ROOT / "tests/fixtures/go_row_prefixes.json").read_text())["rows"]


def _case(entry):
    return entry["row"].replace("{head}", HEAD)


@pytest.mark.parametrize("entry", TABLE, ids=[e["name"] for e in TABLE])
def test_the_fixture_table_gives_the_keepers_verdict(entry):
    ok, reason = go_row_check(_case(entry))
    assert ok is entry["accept"], reason
    if not entry["accept"]:
        assert reason.startswith(entry["reason"] + ":")
        assert entry.get("token", "") in reason


@pytest.mark.parametrize("entry", TABLE, ids=[e["name"] for e in TABLE])
def test_the_cli_gives_the_same_verdict(entry, capsys):
    rc = cli.main(["go-row", "check", "--row", _case(entry)])
    out = capsys.readouterr().out.strip()
    assert (rc, out.split(":")[0]) == ((0, "accept") if entry["accept"] else (2, "refuse"))
    if not entry["accept"]:
        assert entry["reason"] in out and entry.get("token", "") in out


def test_the_cli_reads_the_row_from_stdin(monkeypatch, capsys):
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO("(18:30Z) GO #12\n"))
    assert cli.main(["go-row", "check"]) == 0
    assert capsys.readouterr().out.startswith("accept")


def test_no_second_go_prefix_pattern_in_this_repository():
    """The time-prefix grammar lives in store_waiver.GO_PREFIX only (LE-GO-ROW-PREFIX-PARSERS-DISAGREE)."""
    tracked = subprocess.run(["git", "ls-files", "src", "tools", "rust", "skills"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
    hits = []
    for name in tracked:
        path = ROOT / name
        if path.suffix not in {".py", ".rs", ".sh", ".mjs", ".js", ""} or not path.is_file():
            continue
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if re.search(r"\[0-5\]\[0-9x\]Z|\[0-9x\]Z|, date -u\)\?", line):
                hits.append(f"{name}:{number}")
    assert {h.split(":")[0] for h in hits} == {"src/plangraph/keeper/store_waiver.py"}, hits


def _git(root, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                          cwd=root, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def train_repo(tmp_path):
    """main, then a train branch with three trailered keys: a resolver commit, a part, a checkpoint doc."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base\n\nPlan-Item: plangraph:ALREADY-LANDED")
    _git(root, "checkout", "-q", "-b", "train")
    (root / "src.txt").write_text("part\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "part\n\nPlan-Item: plangraph:PART-KEY")
    (root / "fix.txt").write_text("resolver\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "resolver slice\n\nPlan-Item: plangraph:RESOLVER-KEY")
    (root / "docs/checkpoints").mkdir(parents=True)
    (root / "docs/checkpoints/cp.md").write_text("deferred: LATER-KEY\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "checkpoint\n\nPlan-Item: plangraph:LATER-KEY")
    return root


def _row(keys, plan="plangraph:BATCH"):
    return (f"(10:00Z, date -u) GO #1 GO-HEAD {HEAD}; GO-KEYS [{','.join(keys)}]; "
            f"PLAN {plan} conforms=yes deviations=none")


def test_train_keys_come_from_the_trailers_with_checkpoint_only_keys_deferred(train_repo):
    from plangraph.keeper.evidence_git import GitAdapter
    from plangraph.keeper.evidence_pr import train_trailer_keys
    assert train_trailer_keys(GitAdapter(), str(train_repo), "train", ["main"]) == (
        ["plangraph:PART-KEY", "plangraph:RESOLVER-KEY"], ["plangraph:LATER-KEY"])


def test_a_row_must_name_exactly_the_delivered_trailered_keys(train_repo, capsys):
    delivered = ["plangraph:PART-KEY", "plangraph:RESOLVER-KEY"]
    argv = lambda keys: ["go-row", "check", "--repo", str(train_repo), "--head", "train",
                         "--exclude", "main", "--row", _row(keys)]
    assert cli.main(argv(delivered + ["plangraph:BATCH"])) == 0
    assert capsys.readouterr().out.startswith("accept")
    assert cli.main(argv(["plangraph:PART-KEY", "plangraph:BATCH"])) == 2
    assert "go_row_keys_missing" in (out := capsys.readouterr().out) and "plangraph:RESOLVER-KEY" in out
    assert cli.main(argv(delivered + ["plangraph:LATER-KEY"])) == 2
    assert "deferred" in (out := capsys.readouterr().out) and "plangraph:LATER-KEY" in out
    assert cli.main(argv(delivered + ["plangraph:NEVER-SHIPPED"])) == 2
    assert "no trailered commit" in (out := capsys.readouterr().out) and "plangraph:NEVER-SHIPPED" in out
    assert cli.main(["go-row", "check", "--repo", str(train_repo), "--head", "train", "--exclude",
                     "main", "--row", f"(10:00Z) GO #1 GO-HEAD {HEAD}"]) == 2
    assert "go_row_keys_missing" in capsys.readouterr().out
    assert cli.main(["go-row", "check", "--repo", str(train_repo), "--head", "nope", "--row", "GO"]) == 2
    assert "go_row_train_unreadable" in capsys.readouterr().out


def _part(root, key, name, text="x\n"):
    _git(root, "checkout", "-q", "-b", name, "train")
    (root / f"{name}.txt").write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"{name}\n\nPlan-Item: {key}")
    _git(root, "checkout", "-q", "train")
    _git(root, "merge", "-q", "--no-ff", "-m", f"Train 9: merge {name}", name)


@pytest.fixture
def dropped_repo(train_repo):
    """The train of ``train_repo`` plus a part merge that a revert with Dropped-Part cancels."""
    root = train_repo
    _part(root, "plangraph:DROPPED-KEY", "dropme")
    merge = _git(root, "rev-parse", "HEAD")
    _git(root, "revert", "-m", "1", "--no-edit", merge)
    _git(root, "commit", "-q", "--amend", "-m",
         f"Train 9: drop dropme\n\nThis reverts commit {merge[:12]}.\n\nDropped-Part: plangraph:DROPPED-KEY")
    return root


def _train_keys(root):
    from plangraph.keeper.evidence_git import GitAdapter
    from plangraph.keeper.evidence_pr import train_trailer_keys
    return train_trailer_keys(GitAdapter(), str(root), "train", ["main"])


def test_a_reverted_part_gives_up_its_keys(dropped_repo, capsys):
    assert _train_keys(dropped_repo) == (["plangraph:PART-KEY", "plangraph:RESOLVER-KEY"], ["plangraph:LATER-KEY"])
    row = _row(["plangraph:PART-KEY", "plangraph:RESOLVER-KEY", "plangraph:DROPPED-KEY"])
    assert cli.main(["go-row", "check", "--repo", str(dropped_repo), "--head", "train",
                     "--exclude", "main", "--row", row]) == 2
    assert "plangraph:DROPPED-KEY" in capsys.readouterr().out


def test_a_resolver_commit_after_the_drop_still_delivers_the_key(dropped_repo):
    (dropped_repo / "resolver2.txt").write_text("again\n")
    _git(dropped_repo, "add", "-A")
    _git(dropped_repo, "commit", "-q", "-m", "deliver it again\n\nPlan-Item: plangraph:DROPPED-KEY")
    assert "plangraph:DROPPED-KEY" in _train_keys(dropped_repo)[0]


def test_a_resolver_commit_outside_the_dropped_part_keeps_the_key(train_repo):
    root = train_repo
    (root / "outside.txt").write_text("resolver\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "outside the part\n\nPlan-Item: plangraph:DROPPED-KEY")
    _part(root, "plangraph:DROPPED-KEY", "dropme")
    merge = _git(root, "rev-parse", "HEAD")
    _git(root, "revert", "-m", "1", "--no-edit", merge)
    _git(root, "commit", "-q", "--amend", "-m",
         f"Train 9: drop dropme\n\nThis reverts commit {merge[:12]}.\n\nDropped-Part: plangraph:DROPPED-KEY")
    assert "plangraph:DROPPED-KEY" in _train_keys(root)[0]
