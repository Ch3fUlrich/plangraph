"""D-6517 findings of the UI auto deploy: PR-bound CI evidence, private gate state,
and render only from a recorded verified revision. Helpers come from the sibling suite."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from plangraph.live_follow import Config, ConfigError, is_green, main, record_green
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
