"""D-6517 findings of the UI auto deploy: PR-bound CI evidence, private gate state,
and render only from a recorded verified revision. Helpers come from the sibling suite."""
from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from plangraph.live_follow import Config, ConfigError, export_tree, is_green, main, record_green
from test_live_follow import (  # noqa: F401  (helpers shared with the sibling suite)
    CUR,
    HEAD,
    PH_GREEN,
    PH_RED,
    T0,
    FakeRunner,
    base_env,
    gh_json,
    make_cfg,
    merged_pr,
    pulls_endpoint,
    put_token,
    run_entry,
    runs_endpoint,
    runs_payload,
)


def _merged_red_pr(tmp_path, *entries):
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    fake = FakeRunner()
    fake.script(("gh", "api", pulls_endpoint(HEAD)),
                gh_json([merged_pr(7, HEAD, "main", PH_RED)]))
    fake.script(("gh", "api", runs_endpoint(PH_RED)), gh_json(runs_payload(*entries)))
    return cfg, fake


def test_green_run_of_the_merged_pr_is_green(tmp_path):
    cfg, fake = _merged_red_pr(tmp_path, run_entry(head_sha=PH_RED, pr=7))
    assert is_green(fake, cfg, HEAD) is True


def test_successful_run_of_another_pr_on_the_same_head_cannot_override_a_red_run(tmp_path):
    """PR 7 (into main) failed; PR 9 shares the head sha, targets another branch and is
    newer and green. Only runs bound to PR 7 and the target branch count."""
    cfg, fake = _merged_red_pr(
        tmp_path,
        run_entry(head_sha=PH_RED, conclusion="failure", created_at="2026-10-01T00:00:00Z"),
        run_entry(head_sha=PH_RED, created_at="2026-10-02T00:00:00Z", pr=9, base="release"),
        run_entry(head_sha=PH_RED, created_at="2026-10-03T00:00:00Z", pr=7, base="release"),
        run_entry(head_sha=PH_RED, created_at="2026-10-04T00:00:00Z", pr=9),
        run_entry(head_sha=PH_RED, created_at="2026-10-05T00:00:00Z", pr=None))
    assert is_green(fake, cfg, HEAD) is False


def test_pr_run_without_pull_request_binding_is_not_green(tmp_path):
    cfg, fake = _merged_red_pr(tmp_path, run_entry(head_sha=PH_RED, pr=None))
    assert is_green(fake, cfg, HEAD) is False


# --- gate state stays out of the web output directory -------------------------------------

def test_state_defaults_to_a_private_path_outside_the_output_dir(tmp_path):
    env = base_env(tmp_path, PLANGRAPH_STATE=None, HOME=str(tmp_path / "home"))
    cfg = Config.from_env(env)
    state, out = Path(cfg.state).resolve(), Path(cfg.out).resolve()
    assert out not in state.parents
    assert state.parent == (tmp_path / "home" / ".local" / "state" / "plangraph").resolve()


def test_state_default_honours_xdg_state_home(tmp_path):
    env = base_env(tmp_path, PLANGRAPH_STATE=None, HOME=str(tmp_path / "home"),
                   XDG_STATE_HOME=str(tmp_path / "xdg"))
    assert Path(Config.from_env(env).state) == tmp_path / "xdg" / "plangraph" / "live-follow.json"


def test_state_without_state_or_home_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError) as ei:
        Config.from_env(base_env(tmp_path, PLANGRAPH_STATE=None))
    assert "PLANGRAPH_STATE" in str(ei.value)


@pytest.mark.parametrize("where", ["out/state.json", "out/sub/../state.json"])
def test_state_inside_the_output_dir_is_rejected(tmp_path, where):
    env = base_env(tmp_path, PLANGRAPH_STATE=str(tmp_path / where))
    with pytest.raises(ConfigError) as ei:
        Config.from_env(env)
    assert "PLANGRAPH_STATE" in str(ei.value) and "PLANGRAPH_OUT" in str(ei.value)


def test_state_through_a_symlink_into_the_output_dir_is_rejected(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "out")
    env = base_env(tmp_path, PLANGRAPH_STATE=str(tmp_path / "link" / "state.json"))
    with pytest.raises(ConfigError):
        Config.from_env(env)


def test_main_rejects_state_inside_the_output_dir(tmp_path, capsys):
    env = base_env(tmp_path, PLANGRAPH_STATE=str(tmp_path / "out" / "s.json"))
    assert main(["follow"], env=env, runner=FakeRunner(), now=T0) == 2
    assert "PLANGRAPH_STATE" in capsys.readouterr().err


# --- render only from a recorded verified revision -------------------------------------------

def _verified(tmp_path, capsys, head=CUR, recorded=CUR, rc=0):
    cfg = make_cfg(tmp_path)
    if recorded:
        record_green(cfg, recorded, T0)
    fake = FakeRunner()
    fake.script(("git", "-C", cfg.tree, "rev-parse", "HEAD"), (rc, head + "\n"))
    code = main(["verified"], env=base_env(tmp_path), runner=fake, now=T0)
    return code, capsys.readouterr(), fake


def test_verified_passes_when_the_checkout_is_the_recorded_green_head(tmp_path, capsys):
    code, cap, fake = _verified(tmp_path, capsys)
    assert code == 0
    assert json.loads(cap.out.strip()) == {"status": "verified", "sha": CUR}
    assert fake.gh_calls() == []        # no network: only the record and the checkout


@pytest.mark.parametrize("case", ["other_head", "no_state", "git_failed", "empty_head"])
def test_verified_refuses_an_unrecorded_checkout(tmp_path, capsys, case):
    kwargs = {"other_head": dict(head=HEAD), "no_state": dict(recorded=None),
              "git_failed": dict(rc=1), "empty_head": dict(head="")}[case]
    code, cap, _ = _verified(tmp_path, capsys, **kwargs)
    assert code == 3
    assert json.loads(cap.out.strip())["status"] == "unverified"


def test_verified_never_raises_on_a_broken_state_file(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    Path(cfg.state).write_text("{not json", encoding="utf-8")
    fake = FakeRunner()
    fake.script(("git", "-C", cfg.tree, "rev-parse", "HEAD"), (0, CUR + "\n"))
    assert main(["verified"], env=base_env(tmp_path), runner=fake, now=T0) == 3


def test_state_symlink_inside_the_output_dir_pointing_out_is_rejected(tmp_path):
    """The atomic save replaces the symlink itself, which would put a regular state
    file inside the served directory."""
    (tmp_path / "out").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "out" / "state.json").symlink_to(tmp_path / "elsewhere" / "state.json")
    env = base_env(tmp_path, PLANGRAPH_STATE=str(tmp_path / "out" / "state.json"))
    with pytest.raises(ConfigError) as ei:
        Config.from_env(env)
    assert "PLANGRAPH_OUT" in str(ei.value)


# --- the render reads an exported copy of the verified commit, never the live tree ------------

def _tar(files):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return raw.getvalue()


def _exporting(tmp_path, files, head=CUR, recorded=CUR, archive_rc=0):
    cfg = make_cfg(tmp_path)
    if recorded:
        record_green(cfg, recorded, T0)
    fake = FakeRunner()
    fake.script(("git", "-C", cfg.tree, "rev-parse", "HEAD"), (0, head + "\n"))

    def archive(argv, env, cwd):
        out = Path(argv[argv.index("-o") + 1])
        if archive_rc == 0:
            out.write_bytes(_tar(files))
        return (archive_rc, "")

    fake.script(("git", "-C", cfg.tree, "archive"), archive)
    return cfg, fake


def test_export_extracts_exactly_the_verified_commit(tmp_path):
    cfg, fake = _exporting(tmp_path, {"src/plangraph/mod.py": b"print(1)\n"})
    res = export_tree(cfg, fake)
    assert res["status"] == "verified" and res["sha"] == CUR
    tree = Path(res["tree"])
    assert tree.is_relative_to(Path(cfg.state).parent) and not tree.is_relative_to(cfg.out)
    assert (tree / "src/plangraph/mod.py").read_bytes() == b"print(1)\n"
    archive_call = fake.calls_where("archive")[0]["argv"]
    assert archive_call[-1] == CUR, "archive the recorded sha, not a moving ref"


def test_export_replaces_the_previous_tree_completely(tmp_path):
    cfg, fake = _exporting(tmp_path, {"a.txt": b"new"})
    old = Path(cfg.state).parent / "render-tree"
    old.mkdir(parents=True)
    (old / "stale.txt").write_text("old")
    res = export_tree(cfg, fake)
    assert sorted(p.name for p in Path(res["tree"]).iterdir()) == ["a.txt"]


@pytest.mark.parametrize("case", ["unverified_head", "no_state", "archive_failed", "traversal"])
def test_export_refuses_and_keeps_the_previous_tree(tmp_path, case):
    kwargs = {"unverified_head": dict(head=HEAD), "no_state": dict(recorded=None),
              "archive_failed": dict(archive_rc=1), "traversal": {}}[case]
    files = {"../evil.txt": b"x"} if case == "traversal" else {"a.txt": b"new"}
    cfg, fake = _exporting(tmp_path, files, **kwargs)
    old = Path(cfg.state).parent / "render-tree"
    old.mkdir(parents=True, exist_ok=True)
    (old / "keep.txt").write_text("old")
    res = export_tree(cfg, fake)
    assert res["status"] != "verified" and "tree" not in res
    assert (old / "keep.txt").read_text() == "old"
    assert not (Path(cfg.state).parent.parent / "evil.txt").exists()
    leftovers = [p.name for p in Path(cfg.state).parent.iterdir() if p.name.startswith("render-tree.tmp")]
    assert leftovers == []


def test_main_export_prints_tree_and_short_sha_or_nothing(tmp_path, capsys):
    cfg, fake = _exporting(tmp_path, {"a.txt": b"x"})
    assert main(["export"], env=base_env(tmp_path), runner=fake, now=T0) == 0
    # Only the short sha: the tree is always <state dir>/render-tree, never a printed path.
    assert capsys.readouterr().out == CUR[:10] + "\n"
    assert (Path(cfg.state).parent / "render-tree" / "a.txt").exists()
    cfg2, fake2 = _exporting(tmp_path, {"a.txt": b"x"}, head=HEAD)
    assert main(["export"], env=base_env(tmp_path), runner=fake2, now=T0) == 3
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("where", ["out_is_render_tree", "out_inside_render_tree", "out_contains_render_tree"])
def test_output_dir_overlapping_the_render_tree_is_rejected_by_config(tmp_path, where):
    state_dir = tmp_path / "st"
    out = {"out_is_render_tree": state_dir / "render-tree",
           "out_inside_render_tree": state_dir / "render-tree" / "page",
           "out_contains_render_tree": tmp_path}[where]
    env = base_env(tmp_path, PLANGRAPH_STATE=str(state_dir / "s.json"), PLANGRAPH_OUT=str(out))
    with pytest.raises(ConfigError):
        Config.from_env(env)


def test_export_never_replaces_the_page_directory(tmp_path):
    """A Config built directly (not through from_env) with out == render-tree: nothing is renamed."""
    cfg, fake = _exporting(tmp_path, {"a.txt": b"x"})
    page_dir = Path(cfg.state).parent / "render-tree"
    page_dir.mkdir(parents=True)
    (page_dir / "plan-cdn.html").write_text("published")
    from dataclasses import replace
    res = export_tree(replace(cfg, out=str(page_dir)), fake)
    assert res["status"] != "verified"
    assert (page_dir / "plan-cdn.html").read_text() == "published"
    assert not (page_dir / "a.txt").exists()


def test_export_refuses_a_render_tree_that_overlaps_the_live_checkout(tmp_path):
    cfg, fake = _exporting(tmp_path, {"a.txt": b"x"})
    from dataclasses import replace
    res = export_tree(replace(cfg, tree=str(Path(cfg.state).parent / "render-tree")), fake)
    assert res["status"] != "verified"
