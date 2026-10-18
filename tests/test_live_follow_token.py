"""Task C: sentinel scan of EVERY plangraph.live_follow path with a token loaded.

Complements tests/test_live_follow.py (whose helpers are reused here, imported
as siblings the same way tests/test_apply_batch_queue.py imports
test_apply_batch). Every scenario runs with the token file present, and after
each one :func:`assert_no_leak` scans every sink the module could write:
recorded argv/cwd, stdout, stderr, log records, the state file (read
UNCONDITIONALLY: the scenario declares whether it must exist), the result
dict, and the repr of any exception that escaped. Non-vacuity: the sentinel
must appear in exactly the ``gh`` calls' env (``{"GH_TOKEN": SENTINEL}``) and
every git/alert call must carry env None.

follow() and main() never raise, so on those paths an escaped exception is
impossible by construction (a raising call errors the test before the scan).
check() has no blanket except (only main() shields the render unit), so the
two forced check failures are also run directly; the test-made exception that
escapes there is handed to the helper, which still proves the sentinel
surfaced in no sink and that the shipped main() path neutralises it.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest

from plangraph.live_follow import Config, check, follow, main
from test_live_follow import (  # noqa: F401  (helpers shared with the sibling suite)
    BAD_STAMP,
    CUR,
    HEAD,
    SENTINEL,
    T0,
    FakeRunner,
    base_env,
    expected_alert_line,
    gh_json,
    make_cfg,
    pulls_endpoint,
    put_token,
    run_entry,
    runs_endpoint,
    runs_payload,
    script_git,
    script_green_ci,
    write_page,
    write_state,
)

# state expectations for assert_no_leak
STATE_CREATED = "created"          # the scenario itself wrote the state file
STATE_PREEXISTING = "preexisting"  # scenario pre-created it; must still exist
STATE_ABSENT = "absent"            # the scenario must not have created it
STATE_BLOCKED = "blocked"          # the state path exists as a directory


def assert_no_leak(cfg, fake, capsys, caplog, result=None, *,
                   state=STATE_ABSENT, require_gh=True, exc=None,
                   exc_holds_sentinel=False):
    """Scan every sink for SENTINEL; return the (now-consumed) captured output.

    exc: an exception the scenario deliberately provoked and caught (direct
    check() only). Its repr is scanned too; exc_holds_sentinel=True marks the
    ones whose message the TEST wrote with the sentinel — expected in the
    exception object itself, never in a sink.
    """
    # (1) every recorded call: argv and cwd
    for c in fake.calls:
        assert SENTINEL not in " ".join(c["argv"])
        assert SENTINEL not in (c["cwd"] or "")

    # non-vacuity: the token is loaded and rides ONLY in gh's env
    ghs = fake.gh_calls()
    if require_gh:
        assert ghs, "expected at least one gh call carrying the token"
    for c in ghs:
        assert c["env"] == {"GH_TOKEN": SENTINEL}
    for c in fake.calls:
        if not c["argv"] or c["argv"][0] != "gh":
            assert c["env"] is None

    cap = capsys.readouterr()
    assert SENTINEL not in cap.out      # (2)
    assert SENTINEL not in cap.err      # (3)
    assert SENTINEL not in caplog.text  # (4)

    # (5) state file, unconditional
    p = Path(cfg.state)
    if state in (STATE_CREATED, STATE_PREEXISTING):
        assert p.is_file(), f"state file must exist: {p}"
        text = p.read_text(encoding="utf-8")
        assert SENTINEL not in text
    elif state == STATE_ABSENT:
        assert not p.exists(), "scenario must not have written state"
    elif state == STATE_BLOCKED:
        assert p.is_dir(), "state path is expected to be a directory"

    # (6) the result dict, as JSON and as text
    if result is not None:
        assert SENTINEL not in json.dumps(result)
        assert SENTINEL not in str(result)

    # (7) repr of an escaped exception (none may escape follow/main; a
    # direct-check scenario passes it here instead)
    if exc is not None:
        if exc_holds_sentinel:
            assert SENTINEL in repr(exc)   # test-written message
        else:
            assert SENTINEL not in repr(exc)
    return cap


def _call_maybe_raising(fn, *args):
    """Call fn; return (result, escaped-exception-or-None)."""
    try:
        return fn(*args), None
    except Exception as exc:  # noqa: BLE001 - the scenario inspects it
        return None, exc


class RaisingRunner(FakeRunner):
    """FakeRunner whose FIRST matching call is recorded, then raises a
    RuntimeError whose message contains the sentinel."""

    def __init__(self, pred, message):
        super().__init__()
        self.pred = pred
        self.message = message
        self.fired = False

    def run(self, argv, env=None, cwd=None):
        argv = [str(a) for a in argv]
        if not self.fired and self.pred(argv, env):
            self.fired = True
            self.calls.append({
                "argv": argv,
                "env": dict(env) if env is not None else None,
                "cwd": str(cwd) if cwd is not None else None,
            })
            raise RuntimeError(self.message(argv, env))
        return super().run(argv, env, cwd)


# ---------------------------------------------------------------------------
# follow: every decision path, token loaded

@pytest.mark.parametrize("scenario", [
    "moved",
    "up_to_date_green",
    "up_to_date_not_green",
    "not_green_red",
    "not_green_gh_fail",
    "fetch_failed",
    "git_failed",
    "not_fast_forward",
    "ff_failed",
])
def test_follow_paths_never_leak_token(tmp_path, scenario, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    fake = FakeRunner()

    if scenario == "moved":
        script_git(cfg=cfg, fake=fake)
        script_green_ci(fake)
        expect = {"action": "moved", "reason": "ok", "sha": HEAD}
        state, require_gh = STATE_CREATED, True
    elif scenario == "up_to_date_green":
        script_git(cfg=cfg, fake=fake, cur=CUR, head_out=CUR)
        script_green_ci(fake, sha=CUR)
        expect = {"action": "kept", "reason": "up_to_date", "sha": CUR}
        state, require_gh = STATE_CREATED, True
    elif scenario == "up_to_date_not_green":
        script_git(cfg=cfg, fake=fake, cur=CUR, head_out=CUR)
        fake.script(("gh", "api", pulls_endpoint(CUR)), (1, ""))
        expect = {"action": "kept", "reason": "up_to_date_not_green", "sha": CUR}
        state, require_gh = STATE_ABSENT, True
    elif scenario == "not_green_red":
        script_git(cfg=cfg, fake=fake)
        fake.script(("gh", "api", pulls_endpoint(HEAD)), gh_json([]))
        fake.script(("gh", "api", runs_endpoint(HEAD)),
                    gh_json(runs_payload(
                        run_entry(head_sha=HEAD, conclusion="failure"))))
        expect = {"action": "kept", "reason": "not_green", "sha": HEAD}
        state, require_gh = STATE_ABSENT, True
    elif scenario == "not_green_gh_fail":
        script_git(cfg=cfg, fake=fake)
        fake.script(("gh", "api", pulls_endpoint(HEAD)), (1, ""))
        expect = {"action": "kept", "reason": "not_green", "sha": HEAD}
        state, require_gh = STATE_ABSENT, True
    elif scenario == "fetch_failed":
        fake.script(("git", "-C", cfg.tree, "fetch"), (1, ""))
        expect = {"action": "kept", "reason": "fetch_failed", "sha": None}
        state, require_gh = STATE_ABSENT, False
    elif scenario == "git_failed":
        fake.script(("git", "-C", cfg.tree, "fetch"), (0, ""))
        fake.script(("git", "-C", cfg.tree, "rev-parse", "origin/main"),
                    (128, ""))
        expect = {"action": "kept", "reason": "git_failed", "sha": None}
        state, require_gh = STATE_ABSENT, False
    elif scenario == "not_fast_forward":
        script_git(cfg=cfg, fake=fake, ancestor_rc=1)
        script_green_ci(fake)
        expect = {"action": "kept", "reason": "not_fast_forward", "sha": HEAD}
        state, require_gh = STATE_CREATED, True
    else:  # ff_failed
        script_git(cfg=cfg, fake=fake, merge_rc=1)
        script_green_ci(fake)
        expect = {"action": "kept", "reason": "ff_failed", "sha": HEAD}
        state, require_gh = STATE_CREATED, True

    res = follow(cfg, fake, now=T0)   # follow never raises: a raise errors here
    assert res == expect

    cap = assert_no_leak(cfg, fake, capsys, caplog, result=res,
                         state=state, require_gh=require_gh)
    assert cap.out == "" and cap.err == ""


# ---------------------------------------------------------------------------
# follow: forced exceptions with the sentinel IN the exception message

def test_follow_first_gh_call_raises_token_message(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    fake = RaisingRunner(
        pred=lambda argv, env: bool(argv) and argv[0] == "gh",
        message=lambda argv, env: f"boom {env['GH_TOKEN']}",
    )
    script_git(cfg=cfg, fake=fake)

    res = follow(cfg, fake, now=T0)
    assert fake.fired
    assert res == {"action": "kept", "reason": "error", "sha": None}
    # The escaped exception (if follow had not caught it) would carry the
    # token; follow swallows, so nothing surfaces. The test-made message is
    # reconstructed here only to prove the sink scan is meaningful.
    assert_no_leak(cfg, fake, capsys, caplog, result=res)


def test_follow_merge_call_raises_sentinel_message(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    fake = RaisingRunner(
        pred=lambda argv, env: "merge" in argv and "--ff-only" in argv,
        message=lambda argv, env: f"boom {SENTINEL}",
    )
    script_git(cfg=cfg, fake=fake)
    script_green_ci(fake)

    res = follow(cfg, fake, now=T0)
    assert fake.fired
    assert res == {"action": "kept", "reason": "error", "sha": None}
    # The green head was recorded before the merge blew up.
    assert_no_leak(cfg, fake, capsys, caplog, result=res,
                   state=STATE_CREATED)


def test_follow_state_path_is_directory(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    Path(cfg.state).mkdir()  # record_green's os.replace onto a dir -> OSError
    fake = FakeRunner()
    script_git(cfg=cfg, fake=fake, cur=CUR, head_out=CUR)
    script_green_ci(fake, sha=CUR)

    res = follow(cfg, fake, now=T0)
    assert res == {"action": "kept", "reason": "error", "sha": None}
    assert_no_leak(cfg, fake, capsys, caplog, result=res,
                   state=STATE_BLOCKED)


# ---------------------------------------------------------------------------
# check: statuses, alert argv, stderr echo, missing page

@pytest.mark.parametrize("scenario", [
    "ok", "stale_grace", "alerted", "alert_rc1", "page_missing",
])
def test_check_paths_never_leak_token(tmp_path, scenario, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    put_token(tmp_path)
    fake = FakeRunner()
    if scenario in ("alerted", "alert_rc1"):
        cfg = make_cfg(tmp_path, alert_cmd=["notify"])
        fake.script(("notify",), (0, "") if scenario == "alerted" else (1, ""))
    else:
        cfg = make_cfg(tmp_path)
    write_state(cfg, HEAD, T0)
    if scenario == "ok":
        write_page(cfg, HEAD[:7])
    elif scenario in ("stale_grace", "alerted", "alert_rc1"):
        write_page(cfg, BAD_STAMP)
    # page_missing: no page file at all

    now = {"ok": T0 + 10, "stale_grace": T0 + 600}.get(scenario, T0 + 3600)
    res = check(cfg, fake, now=now)

    if scenario == "ok":
        assert res == {"status": "ok", "page": HEAD[:7], "green": HEAD}
        assert fake.calls == []
        state, expect_err = STATE_PREEXISTING, ""
    elif scenario == "stale_grace":
        assert res == {"status": "stale", "page": BAD_STAMP, "green": HEAD}
        assert fake.calls == []
        state, expect_err = STATE_PREEXISTING, ""
    elif scenario == "alerted":
        assert res == {"status": "alerted", "page": BAD_STAMP, "green": HEAD}
        alerts = fake.calls_where("notify")
        assert len(alerts) == 1  # argv content + env None are scanned by helper
        assert alerts[0]["argv"] == [
            "notify", expected_alert_line(BAD_STAMP, HEAD, 60)]
        state, expect_err = STATE_CREATED, ""
    elif scenario == "alert_rc1":
        assert res == {"status": "stale", "page": BAD_STAMP, "green": HEAD}
        alerts = fake.calls_where("notify")
        assert len(alerts) == 1
        assert alerts[0]["argv"] == [
            "notify", expected_alert_line(BAD_STAMP, HEAD, 60)]
        # failed alert is echoed for the next tick
        state, expect_err = STATE_PREEXISTING, \
            expected_alert_line(BAD_STAMP, HEAD, 60)
    else:  # page_missing
        assert res == {"status": "alerted", "page": None, "green": HEAD}
        assert fake.calls == []
        state, expect_err = STATE_CREATED, "page=none"

    cap = assert_no_leak(cfg, fake, capsys, caplog, result=res,
                         state=state, require_gh=False)
    assert cap.out == ""
    if expect_err:
        assert expect_err in cap.err
    else:
        assert cap.err == ""


# ---------------------------------------------------------------------------
# check: forced exceptions (direct call + the shipped main() wrapper)

def test_check_alert_runner_raises_sentinel_message(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    cfg = make_cfg(tmp_path, alert_cmd=["notify"])
    put_token(tmp_path)

    def build():
        return RaisingRunner(
            pred=lambda argv, env: bool(argv) and argv[0] == "notify",
            message=lambda argv, env: f"alert boom {SENTINEL}",
        )

    def armed(cfg, fake):
        write_state(cfg, HEAD, T0)
        write_page(cfg, BAD_STAMP)
        return fake

    # direct check(): the exception escapes (check has no blanket except;
    # only main() shields the render unit) — nothing may surface in a sink
    fake = armed(cfg, build())
    res, exc = _call_maybe_raising(check, cfg, fake, T0 + 3600)
    assert fake.fired
    cap = assert_no_leak(cfg, fake, capsys, caplog, result=res,
                         state=STATE_PREEXISTING, require_gh=False,
                         exc=exc, exc_holds_sentinel=True)
    assert exc is not None and res is None
    assert "Traceback" not in cap.out + cap.err

    # the shipped path must neutralise the very same failure
    fake2 = armed(cfg, build())
    rc = main(["check"], env=base_env(tmp_path, PLANGRAPH_ALERT_CMD="notify"),
              runner=fake2, now=T0 + 3600)
    cap2 = assert_no_leak(cfg, fake2, capsys, caplog,
                          state=STATE_PREEXISTING, require_gh=False)
    assert rc == 0
    assert cap2.err == ""
    assert "Traceback" not in cap2.out
    assert json.loads(cap2.out.strip()) == {"status": "error"}


def test_check_state_unwritable_after_alert(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    cfg = make_cfg(tmp_path, alert_cmd=["notify"])
    put_token(tmp_path)

    def eat_state(argv, env, cwd):
        # "succeeds", but replaces the state file with a directory: the
        # subsequent _save_state raises OSError through check().
        p = Path(cfg.state)
        p.unlink()
        p.mkdir()
        return (0, "")

    def armed():
        fake = FakeRunner()
        fake.script(("notify",), eat_state)
        write_state(cfg, HEAD, T0)
        write_page(cfg, BAD_STAMP)
        return fake

    fake = armed()
    res, exc = _call_maybe_raising(check, cfg, fake, T0 + 3600)
    assert isinstance(exc, OSError)  # documented propagation, not a leak sink
    cap = assert_no_leak(cfg, fake, capsys, caplog,
                         result=res, state=STATE_BLOCKED, require_gh=False,
                         exc=exc)
    assert res is None
    assert cap.out == "" and cap.err == ""
    assert "Traceback" not in cap.out + cap.err

    # main() must stay quiet on the identical failure
    shutil.rmtree(cfg.state)
    fake2 = armed()
    rc = main(["check"], env=base_env(tmp_path, PLANGRAPH_ALERT_CMD="notify"),
              runner=fake2, now=T0 + 3600)
    cap2 = assert_no_leak(cfg, fake2, capsys, caplog,
                          state=STATE_BLOCKED, require_gh=False)
    assert rc == 0
    assert "Traceback" not in cap2.err
    assert json.loads(cap2.out.strip()) == {"status": "error"}


# ---------------------------------------------------------------------------
# main(): success and failure paths with a token loaded

def test_main_follow_moved_with_token_one_json_line(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    env = base_env(tmp_path)
    put_token(tmp_path)
    cfg = Config.from_env(env)
    fake = FakeRunner()
    script_git(cfg=cfg, fake=fake)
    script_green_ci(fake)

    rc = main(["follow"], env=env, runner=fake, now=T0)
    assert rc == 0
    cap = assert_no_leak(cfg, fake, capsys, caplog,
                         state=STATE_CREATED, require_gh=True)
    lines = cap.out.strip().splitlines()
    assert len(lines) == 1
    doc = json.loads(lines[0])
    assert doc == {"action": "moved", "reason": "ok", "sha": HEAD}
    # re-scan with the parsed result dict included
    assert_no_leak(cfg, fake, capsys, caplog, result=doc,
                   state=STATE_CREATED, require_gh=True)
    assert cap.err == ""


def test_main_check_ok_path(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    env = base_env(tmp_path)
    put_token(tmp_path)
    cfg = Config.from_env(env)
    write_state(cfg, HEAD, T0)
    write_page(cfg, HEAD[:7])
    fake = FakeRunner()

    rc = main(["check"], env=env, runner=fake, now=T0 + 10)
    assert rc == 0
    cap = assert_no_leak(cfg, fake, capsys, caplog,
                         state=STATE_PREEXISTING, require_gh=False)
    assert cap.err == ""
    lines = cap.out.strip().splitlines()
    assert len(lines) == 1
    doc = json.loads(lines[0])
    assert doc == {"status": "ok", "page": HEAD[:7], "green": HEAD}
    assert_no_leak(cfg, fake, capsys, caplog, result=doc,
                   state=STATE_PREEXISTING, require_gh=False)


def test_main_follow_runner_raises_sentinel_message(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    env = base_env(tmp_path)
    put_token(tmp_path)
    cfg = Config.from_env(env)
    fake = RaisingRunner(
        pred=lambda argv, env_: bool(argv) and argv[0] == "gh",
        message=lambda argv, env_: f"boom {env_['GH_TOKEN']}",
    )
    script_git(cfg=cfg, fake=fake)

    rc = main(["follow"], env=env, runner=fake, now=T0)
    assert rc == 0
    cap = assert_no_leak(cfg, fake, capsys, caplog,
                         state=STATE_ABSENT, require_gh=True)
    assert cap.err == ""
    lines = cap.out.strip().splitlines()
    assert len(lines) == 1
    doc = json.loads(lines[0])
    assert doc == {"action": "kept", "reason": "error", "sha": None}
    assert "Traceback" not in cap.out + cap.err
    assert_no_leak(cfg, fake, capsys, caplog, result=doc,
                   state=STATE_ABSENT, require_gh=True)


def test_main_check_runner_raises_sentinel_message(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    env = base_env(tmp_path, PLANGRAPH_ALERT_CMD="notify")
    put_token(tmp_path)
    cfg = Config.from_env(env)
    write_state(cfg, HEAD, T0)
    write_page(cfg, BAD_STAMP)
    fake = RaisingRunner(
        pred=lambda argv, env_: bool(argv) and argv[0] == "notify",
        message=lambda argv, env_: f"alert boom {SENTINEL}",
    )

    rc = main(["check"], env=env, runner=fake, now=T0 + 3600)
    assert rc == 0
    cap = assert_no_leak(cfg, fake, capsys, caplog,
                         state=STATE_PREEXISTING, require_gh=False)
    assert cap.err == ""
    lines = cap.out.strip().splitlines()
    assert len(lines) == 1
    doc = json.loads(lines[0])
    assert doc == {"status": "error"}
    assert "Traceback" not in cap.out + cap.err
    assert_no_leak(cfg, fake, capsys, caplog, result=doc,
                   state=STATE_PREEXISTING, require_gh=False)


def test_main_config_error_with_token_file_exits_two(tmp_path, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    put_token(tmp_path)  # the token exists and is never read on this path
    env = base_env(tmp_path, PLANGRAPH_REPO=None)
    fake = FakeRunner()

    rc = main(["follow"], env=env, runner=fake, now=T0)
    assert rc == 2
    cap = capsys.readouterr()
    # no Config was built: scan every sink directly
    assert SENTINEL not in cap.out
    assert SENTINEL not in cap.err
    assert SENTINEL not in caplog.text
    assert fake.calls == []
    assert not Path(tmp_path / "state.json").exists()
    assert "PLANGRAPH_REPO" in cap.err
    assert "Traceback" not in cap.err
