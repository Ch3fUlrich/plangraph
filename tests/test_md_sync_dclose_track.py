"""D-3393: a Closes key@sha whose commit lives only in the item's track repo
(keeper.json repos '<project>/<track>') closes; the primary repo still wins."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_md_sync_dclose import (  # noqa: E402
    assert_ops_valid, build, close_ops, commit_ref, dec_row, decs, item, repo)

__all__ = ["repo"]


@pytest.fixture
def track(tmp_path):
    """A second temp git repo: one commit on main, one on a side branch."""
    path = tmp_path / "track"
    path.mkdir()

    def run(*args):
        out = subprocess.run(["git", *args], cwd=path, capture_output=True,
                             text=True)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    run("init", "-q", "-b", "main")
    run("config", "user.email", "synthetic@example.invalid")
    run("config", "user.name", "synthetic")
    (path / "t.txt").write_text("track\n", encoding="utf-8")
    run("add", "t.txt")
    run("commit", "-q", "-m", "track")
    merged = run("rev-parse", "HEAD")
    run("checkout", "-q", "-b", "side")
    (path / "u.txt").write_text("side\n", encoding="utf-8")
    run("add", "u.txt")
    run("commit", "-q", "-m", "side")
    unmerged = run("rev-parse", "HEAD")
    run("checkout", "-q", "main")
    return SimpleNamespace(path=str(path), merged=merged, unmerged=unmerged)


def both(repo, track):
    return {"demo": {"path": repo.path, "ref": "main"},
            "demo/ROUTING": {"path": track.path, "ref": "main"}}


def titem(ident, track_name="ROUTING", **kw):
    out = item(ident, kinds=("commit",), **kw)
    out["track"] = track_name
    return out


def test_track_only_commit_merged_closes_with_track_repo(repo, track, tmp_path):
    text, view = decs(tmp_path, dec_row("D-3393", "ledger prep",
                                        f"demo:LEDGER@{track.merged[:8]}"))
    p = build([titem("LEDGER")], text, view, repo, repos=both(repo, track))
    assert len(close_ops(p.ops)) == 1
    commit = commit_ref(p.ops)
    assert commit["ref"]["sha"] == track.merged
    assert commit["repo"] == "demo/ROUTING"
    assert track.path not in str(p.ops)
    assert_ops_valid(p.ops)


def test_track_commit_unmerged_stays_in_review(repo, track, tmp_path):
    text, view = decs(tmp_path, dec_row("D-3393", "ledger prep",
                                        f"demo:LEDGER@{track.unmerged[:8]}"))
    p = build([titem("LEDGER")], text, view, repo, repos=both(repo, track))
    assert close_ops(p.ops) == []
    assert p.d_in_review[0]["reason"] == "not on main"


def test_item_without_track_ignores_track_repo(repo, track, tmp_path):
    text, view = decs(tmp_path, dec_row("D-3393", "ledger prep",
                                        f"demo:LEDGER@{track.merged[:8]}"))
    plain = item("LEDGER", kinds=("commit",))
    p = build([plain], text, view, repo, repos=both(repo, track))
    assert close_ops(p.ops) == []


def test_unregistered_track_repo_fails_closed(repo, track, tmp_path):
    text, view = decs(tmp_path, dec_row("D-3393", "ledger prep",
                                        f"demo:LEDGER@{track.merged[:8]}"))
    p = build([titem("LEDGER", track_name="OTHER")], text, view, repo,
              repos=both(repo, track))
    assert close_ops(p.ops) == []


def test_primary_repo_wins_when_it_holds_the_commit(repo, track, tmp_path):
    text, view = decs(tmp_path, dec_row("D-3393", "ship",
                                        f"demo:LEDGER@{repo.merged[:8]}"))
    p = build([titem("LEDGER")], text, view, repo, repos=both(repo, track))
    assert commit_ref(p.ops)["repo"] == "demo"
    assert len(close_ops(p.ops)) == 1


def test_unsafe_track_value_is_ignored(repo, track, tmp_path):
    text, view = decs(tmp_path, dec_row("D-3393", "ledger prep",
                                        f"demo:LEDGER@{track.merged[:8]}"))
    p = build([titem("LEDGER", track_name="../ROUTING")], text, view, repo,
              repos={**both(repo, track), "demo/../ROUTING": track.path})
    assert close_ops(p.ops) == []
