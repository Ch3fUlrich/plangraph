"""Failing-first tests for plangraph.live_follow (T: live green-main follower).

Every outside effect (git, gh, alert commands) goes through ``FakeRunner``,
which records each call and answers from a table keyed by an argv prefix;
unknown argv answers (127, "").  No real git, gh or network is used.
Synthetic repo "acme/widgets"; the token sentinel appears only inside
tmp_path files and the env passed to gh.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from plangraph import live_follow
from plangraph.live_follow import (
    Config,
    ConfigError,
    check,
    follow,
    gh_api,
    is_green,
    load_state,
    main,
    page_stamp,
    read_token,
    record_green,
)

SENTINEL = "ghs_TESTSENTINEL0123456789"
REPO = "acme/widgets"
WF = "tests"

# 40-hex shas (all distinct prefixes, none a prefix of another)
CUR = "00112233445566778899aabbccddeeff00112233"
HEAD = "aa11bb22cc33dd44ee55ff660011223344556677"
HEAD2 = "bb22cc33dd44ee55ff660011223344556677aa11"
PR_HEAD = "cc33dd44ee55ff660011223344556677aa11bb22"
STAMP_SHA = "abc1234567" + "d" * 30
BAD_STAMP = "ffffffffffff"

T0 = 1_000_000.0


def norm(tok: str) -> str:
    t = str(tok)
    if "/" in t or "\\" in t:
        return os.path.normpath(t)
    return t


class FakeRunner:
    """Scripted stand-in for live_follow.Runner; records every call.

    table maps an argv prefix (tuple of strings) to a response:
      (rc, stdout) | [(rc, stdout), ...] consumed in order, last repeats |
      callable(argv, env, cwd) -> (rc, stdout)
    Unknown argv -> (127, "").
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.table: dict[tuple, object] = {}
        self._hits: dict[tuple, int] = {}

    def script(self, prefix, response):
        key = tuple(norm(t) for t in prefix)
        self.table[key] = response
        self._hits[key] = 0
        return self

    def run(self, argv, env=None, cwd=None):
        argv = [str(a) for a in argv]
        self.calls.append(
            {
                "argv": argv,
                "env": dict(env) if env is not None else None,
                "cwd": str(cwd) if cwd is not None else None,
            }
        )
        na = [norm(a) for a in argv]
        best_key, best_len = None, -1
        for key in self.table:
            n = len(key)
            if n <= len(na) and na[:n] == list(key) and n > best_len:
                best_key, best_len = key, n
        if best_key is None:
            return (127, "")
        resp = self.table[best_key]
        if callable(resp):
            return tuple(resp(argv, env, cwd))
        if isinstance(resp, list):
            i = self._hits[best_key]
            self._hits[best_key] = i + 1
            return tuple(resp[min(i, len(resp) - 1)])
        return tuple(resp)

    # conveniences --------------------------------------------------------
    def calls_where(self, *tokens):
        return [
            c for c in self.calls
            if all(t in c["argv"] for t in tokens)
        ]

    def merge_calls(self):
        return [c for c in self.calls if "merge" in c["argv"]]

    def gh_calls(self):
        return [c for c in self.calls if c["argv"] and c["argv"][0] == "gh"]


def make_cfg(tmp_path: Path, **over) -> Config:
    base = {
        "tree": str(tmp_path / "tree"),
        "repo": REPO,
        "token_file": str(tmp_path / "env.sh"),
        "out": str(tmp_path / "out"),
        "state": str(tmp_path / "state.json"),
        "workflow": WF,
    }
    base.update(over)
    return Config(**base)


def put_token(tmp_path: Path, value: str = SENTINEL) -> Path:
    p = tmp_path / "env.sh"
    p.write_text(f"# ci token file\nexport GH_TOKEN='{value}'\n", encoding="utf-8")
    return p


def pulls_endpoint(sha: str) -> str:
    return f"repos/{REPO}/commits/{sha}/pulls"


def runs_endpoint(sha: str) -> str:
    return f"repos/{REPO}/actions/runs?head_sha={sha}&per_page=30"


def gh_json(payload):
    return (0, json.dumps(payload))


def run_entry(name=WF, status="completed", conclusion="success",
              created_at="2026-10-01T00:00:00Z"):
    return {"name": name, "status": status, "conclusion": conclusion,
            "created_at": created_at}


def runs_payload(*entries):
    return {"workflow_runs": list(entries)}


def pr(number, merge_commit_sha, base_ref, head_sha):
    return {"number": number, "merge_commit_sha": merge_commit_sha,
            "base": {"ref": base_ref}, "head": {"sha": head_sha}}


def script_git(fake: FakeRunner, cfg: Config, cur=CUR,
               ancestor_rc=0, merge_rc=0, head_out=HEAD):
    """Standard happy git scripts; head_out lets tests move the ref."""
    t = cfg.tree
    fake.script(("git", "-C", t, "fetch"), (0, ""))
    fake.script(("git", "-C", t, "rev-parse", f"{cfg.remote}/{cfg.branch}"),
                (0, head_out + "\n"))
    fake.script(("git", "-C", t, "rev-parse", "HEAD"), (0, cur + "\n"))
    fake.script(("git", "-C", t, "merge-base", "--is-ancestor"), (ancestor_rc, ""))
    fake.script(("git", "-C", t, "merge", "--ff-only"), (merge_rc, ""))


def script_green_ci(fake: FakeRunner, sha=HEAD, candidate=PR_HEAD,
                    entries=None):
    fake.script(("gh", "api", pulls_endpoint(sha)),
                gh_json([pr(7, sha, "main", candidate)]))
    fake.script(("gh", "api", runs_endpoint(candidate)),
                gh_json(entries or runs_payload(run_entry())))


def expected_alert_line(page, green_sha, minutes):
    return (f"plangraph page stale: page={page or 'none'} "
            f"main-green={green_sha[:10]} for {minutes} min")


def write_page(cfg: Config, stamp: str) -> Path:
    out = Path(cfg.out)
    out.mkdir(parents=True, exist_ok=True)
    p = out / "plan-cdn.html"
    p.write_text(
        f'<html><head><meta name="plangraph-build" content="{stamp}">'
        "</head><body>x</body></html>",
        encoding="utf-8",
    )
    return p


def write_state(cfg: Config, green_sha, green_since, alerted=None):
    Path(cfg.state).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.state).write_text(
        json.dumps({"green_sha": green_sha, "green_since": green_since,
                    "alerted": alerted}),
        encoding="utf-8",
    )


def base_env(tmp_path: Path, **over):
    env = {
        "PLANGRAPH_LIVE_TREE": str(tmp_path / "tree"),
        "PLANGRAPH_REPO": REPO,
        "PLANGRAPH_GH_TOKEN_FILE": str(tmp_path / "env.sh"),
        "PLANGRAPH_OUT": str(tmp_path / "out"),
        "PLANGRAPH_STATE": str(tmp_path / "state.json"),
    }
    env.update(over)
    for k, v in list(env.items()):
        if v is None:
            del env[k]
    return env


# ---------------------------------------------------------------------------
# 1. green PR head moves the tree

def test_green_pr_head_moves_tree_with_exact_verified_sha(tmp_path):
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    fake = FakeRunner()
    script_git(cfg=cfg, fake=fake)
    script_green_ci(fake)

    res = follow(cfg, fake, now=T0)

    assert res["action"] == "moved"
    assert res["sha"] == HEAD
    merges = fake.merge_calls()
    assert len(merges) == 1
    assert merges[0]["argv"][-1] == HEAD          # exact verified sha, stripped
    assert "--ff-only" in merges[0]["argv"]
    state = load_state(cfg)
    assert state["green_sha"] == HEAD
    assert state["green_since"] == T0


# ---------------------------------------------------------------------------
# 2. non-green states keep the tree

@pytest.mark.parametrize("case", ["red", "pending", "no_runs", "wrong_workflow",
                                  "api_fail_pulls", "api_fail_runs"])
def test_non_green_never_merges(tmp_path, case):
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    fake = FakeRunner()
    script_git(cfg=cfg, fake=fake)
    fake.script(("gh", "api", pulls_endpoint(HEAD)), gh_json([]))
    runs_key = ("gh", "api", runs_endpoint(HEAD))
    if case == "red":
        fake.script(runs_key, gh_json(runs_payload(
            run_entry(conclusion="failure"))))
    elif case == "pending":
        fake.script(runs_key, gh_json(runs_payload(
            run_entry(status="in_progress", conclusion=None))))
    elif case == "no_runs":
        fake.script(runs_key, gh_json(runs_payload()))
    elif case == "wrong_workflow":
        fake.script(runs_key, gh_json(runs_payload(
            run_entry(name="lint"))))
    elif case == "api_fail_pulls":
        fake.script(("gh", "api", pulls_endpoint(HEAD)), (1, ""))
    elif case == "api_fail_runs":
        fake.script(runs_key, (1, ""))

    res = follow(cfg, fake, now=T0)

    assert res["action"] == "kept"
    assert res["reason"] == "not_green"
    assert fake.merge_calls() == []
    assert load_state(cfg) == {}                   # state untouched


# ---------------------------------------------------------------------------
# 3. commit without a PR

def test_commit_without_pr_needs_its_own_green_run(tmp_path):
    withrun = make_cfg(tmp_path, state=str(tmp_path / "s1.json"))
    norun = make_cfg(tmp_path, state=str(tmp_path / "s2.json"))
    put_token(tmp_path)
    for cfg, green in ((withrun, True), (norun, False)):
        fake = FakeRunner()
        script_git(cfg=cfg, fake=fake, cur=CUR)
        fake.script(("gh", "api", pulls_endpoint(HEAD)), gh_json([]))
        if green:
            fake.script(("gh", "api", runs_endpoint(HEAD)),
                        gh_json(runs_payload(run_entry())))
        else:
            fake.script(("gh", "api", runs_endpoint(HEAD)),
                        gh_json(runs_payload()))
        res = follow(cfg, fake, now=T0)
        if green:
            assert res["action"] == "moved"
            assert res["sha"] == HEAD
        else:
            assert res["action"] == "kept"
            assert res["reason"] == "not_green"
            assert fake.merge_calls() == []


def test_nonempty_pr_list_without_candidate_is_not_green(tmp_path):
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    fake = FakeRunner()
    script_git(cfg=cfg, fake=fake)
    fake.script(("gh", "api", pulls_endpoint(HEAD)),
                gh_json([pr(9, "ffffffff" * 5, "feature/x", "eeee" * 10)]))

    res = follow(cfg, fake, now=T0)
    assert res["action"] == "kept"
    assert res["reason"] == "not_green"
    assert fake.merge_calls() == []


# ---------------------------------------------------------------------------
# 4. newest run wins

@pytest.mark.parametrize("older,newer,green", [
    (run_entry(conclusion="success"),
     run_entry(created_at="2026-10-02T00:00:00Z", conclusion="failure"), False),
    (run_entry(conclusion="failure"),
     run_entry(created_at="2026-10-02T00:00:00Z", conclusion="success"), True),
])
def test_newest_run_decides_green(tmp_path, older, newer, green):
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    fake = FakeRunner()
    fake.script(("gh", "api", pulls_endpoint(HEAD)), gh_json([]))
    fake.script(("gh", "api", runs_endpoint(HEAD)), gh_json(runs_payload(older, newer)))
    assert is_green(fake, cfg, HEAD) is green


# ---------------------------------------------------------------------------
# 5. not a fast-forward

def test_not_fast_forward_keeps_tree(tmp_path):
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    fake = FakeRunner()
    script_git(cfg=cfg, fake=fake, ancestor_rc=1)

    res = follow(cfg, fake, now=T0)
    assert res["action"] == "kept"
    assert res["reason"] == "not_fast_forward"
    assert fake.merge_calls() == []


# ---------------------------------------------------------------------------
# 6. fetch failure / missing token: kept, never raises

def test_fetch_failure_keeps_tree(tmp_path):
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    fake = FakeRunner()
    fake.script(("git", "-C", cfg.tree, "fetch"), (1, ""))
    res = follow(cfg, fake, now=T0)
    assert res == {"action": "kept", "reason": "fetch_failed", "sha": None}


def test_missing_token_keeps_tree_and_skips_gh(tmp_path):
    cfg = make_cfg(tmp_path)  # env.sh never written
    fake = FakeRunner()
    script_git(cfg=cfg, fake=fake)
    res = follow(cfg, fake, now=T0)
    assert res["action"] == "kept"
    assert res["reason"] == "no_token"
    assert fake.gh_calls() == []
    assert fake.merge_calls() == []


def test_follow_never_raises_on_unscripted_environment(tmp_path):
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    fake = FakeRunner()  # everything answers (127, "")
    res = follow(cfg, fake, now=T0)
    assert res["action"] == "kept"
    assert isinstance(res["reason"], str)


def test_git_rev_parse_failure_keeps_tree(tmp_path):
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    fake = FakeRunner()
    fake.script(("git", "-C", cfg.tree, "fetch"), (0, ""))
    fake.script(("git", "-C", cfg.tree, "rev-parse", "origin/main"), (128, ""))
    res = follow(cfg, fake, now=T0)
    assert res["action"] == "kept"
    assert res["reason"] == "git_failed"
    assert fake.merge_calls() == []


# ---------------------------------------------------------------------------
# 7. ff-only merge failure

def test_ff_merge_failure_keeps_tree(tmp_path):
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    fake = FakeRunner()
    script_git(cfg=cfg, fake=fake, merge_rc=1)
    script_green_ci(fake)
    res = follow(cfg, fake, now=T0)
    assert res["action"] == "kept"
    assert res["reason"] == "ff_failed"
    assert load_state(cfg) == {}


# ---------------------------------------------------------------------------
# 8. merge uses the verified sha, not the moving ref

def test_merge_uses_verified_sha_not_ref_reresolved(tmp_path):
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    fake = FakeRunner()
    t = cfg.tree
    fake.script(("git", "-C", t, "fetch"), (0, ""))
    # the ref moves: a second rev-parse of origin/main would return HEAD2
    fake.script(("git", "-C", t, "rev-parse", "origin/main"),
                [(0, HEAD + "\n"), (0, HEAD2 + "\n")])
    fake.script(("git", "-C", t, "rev-parse", "HEAD"), (0, CUR + "\n"))
    fake.script(("git", "-C", t, "merge-base", "--is-ancestor"), (0, ""))
    fake.script(("git", "-C", t, "merge", "--ff-only"), (0, ""))
    script_green_ci(fake)

    res = follow(cfg, fake, now=T0)
    assert res["action"] == "moved"
    assert res["sha"] == HEAD
    merges = fake.merge_calls()
    assert len(merges) == 1
    assert merges[0]["argv"][-1] == HEAD
    assert HEAD2 not in " ".join(merges[0]["argv"])


# ---------------------------------------------------------------------------
# 9. token never leaks

@pytest.mark.parametrize("scenario", ["moved", "gh_fail", "no_token"])
def test_token_never_leaks(tmp_path, scenario, capsys):
    cfg = make_cfg(tmp_path)
    fake = FakeRunner()
    if scenario == "no_token":
        pass  # deliberately no token file
    else:
        put_token(tmp_path)
    script_git(cfg=cfg, fake=fake)
    if scenario == "moved":
        script_green_ci(fake)
    elif scenario == "gh_fail":
        fake.script(("gh", "api", pulls_endpoint(HEAD)), (1, ""))

    res = follow(cfg, fake, now=T0)
    captured = capsys.readouterr()
    assert res["action"] in ("moved", "kept")

    assert SENTINEL not in captured.out
    assert SENTINEL not in captured.err
    for c in fake.calls:
        assert SENTINEL not in " ".join(c["argv"])
        assert SENTINEL not in (c["cwd"] or "")
        for k, v in (c["env"] or {}).items():
            if k not in ("GH_TOKEN", "GITHUB_TOKEN"):
                assert SENTINEL not in str(v)
    if Path(cfg.state).exists():
        assert SENTINEL not in Path(cfg.state).read_text(encoding="utf-8")
    if scenario == "moved":
        ghs = fake.gh_calls()
        assert ghs, "success path must have called gh"
        for c in ghs:
            assert c["env"] is not None and c["env"]["GH_TOKEN"] == SENTINEL
    elif scenario == "gh_fail":
        c = fake.gh_calls()[0]
        assert c["env"] == {"GH_TOKEN": SENTINEL}
    else:
        assert fake.gh_calls() == []


def test_gh_api_contract(tmp_path):
    cfg = make_cfg(tmp_path)
    put_token(tmp_path)
    fake = FakeRunner()
    fake.script(("gh", "api", "repos/x/y/thing"), gh_json({"a": 1}))
    assert gh_api(fake, cfg, "repos/x/y/thing") == {"a": 1}
    fake.script(("gh", "api", "repos/x/y/list"), gh_json([1, 2]))
    assert gh_api(fake, cfg, "repos/x/y/list") == {"items": [1, 2]}
    fake.script(("gh", "api", "repos/x/y/rc1"), (1, "{}"))
    assert gh_api(fake, cfg, "repos/x/y/rc1") is None
    fake.script(("gh", "api", "repos/x/y/empty"), (0, ""))
    assert gh_api(fake, cfg, "repos/x/y/empty") is None
    fake.script(("gh", "api", "repos/x/y/bad"), (0, "not json{"))
    assert gh_api(fake, cfg, "repos/x/y/bad") is None
    res = gh_api(fake, cfg, "repos/x/y/unknown")   # (127, "")
    assert res is None
    for c in fake.gh_calls():
        assert SENTINEL not in " ".join(c["argv"])
    ncfg = make_cfg(tmp_path, token_file=str(tmp_path / "nope.sh"))
    assert gh_api(fake, ncfg, "repos/x/y/thing") is None


# ---------------------------------------------------------------------------
# 10. read_token

def test_read_token_formats(tmp_path):
    p = tmp_path / "tok.sh"
    p.write_text("export GH_TOKEN=plain1\n", encoding="utf-8")
    assert read_token(str(p)) == "plain1"
    p.write_text("GH_TOKEN='sq'\n", encoding="utf-8")
    assert read_token(str(p)) == "sq"
    p.write_text('GH_TOKEN="dq"\n', encoding="utf-8")
    assert read_token(str(p)) == "dq"
    p.write_text("# comment\n\n   \nexport GITHUB_TOKEN='gtok'\n", encoding="utf-8")
    assert read_token(str(p)) == "gtok"
    p.write_text("GITHUB_TOKEN=second\nGH_TOKEN=first\n", encoding="utf-8")
    assert read_token(str(p)) == "first"
    assert read_token(str(tmp_path / "absent.sh")) is None
    p.write_text("OTHER_KEY=zz\n", encoding="utf-8")
    assert read_token(str(p)) is None
    p.write_text("GH_TOKEN=\n", encoding="utf-8")
    assert read_token(str(p)) is None
    p.write_text('GH_TOKEN=""\n', encoding="utf-8")
    assert read_token(str(p)) is None


# ---------------------------------------------------------------------------
# 11. record_green / load_state

def test_record_green_keeps_green_since_when_sha_unchanged(tmp_path):
    cfg = make_cfg(tmp_path, state=str(tmp_path / "deep" / "nested" / "st.json"))
    record_green(cfg, HEAD, now=T0)
    st = load_state(cfg)
    assert st["green_sha"] == HEAD
    assert st["green_since"] == T0
    assert st["alerted"] is None
    assert Path(cfg.state).exists()                # parent dirs created

    write_state(cfg, HEAD, T0, alerted=HEAD)       # simulate an alert
    record_green(cfg, HEAD, now=T0 + 500)
    st = load_state(cfg)
    assert st["green_since"] == T0
    assert st["alerted"] == HEAD                   # unchanged sha keeps both


def test_record_green_resets_when_sha_changes(tmp_path):
    cfg = make_cfg(tmp_path)
    write_state(cfg, HEAD, T0, alerted=HEAD)
    record_green(cfg, HEAD2, now=T0 + 900)
    st = load_state(cfg)
    assert st["green_sha"] == HEAD2
    assert st["green_since"] == T0 + 900
    assert st["alerted"] is None


def test_load_state_corrupt_file_is_empty(tmp_path):
    cfg = make_cfg(tmp_path)
    Path(cfg.state).write_text("{not json at all", encoding="utf-8")
    assert load_state(cfg) == {}
    assert load_state(make_cfg(tmp_path, state=str(tmp_path / "gone.json"))) == {}


# ---------------------------------------------------------------------------
# 12. page_stamp

def test_page_stamp_parses_meta_tag(tmp_path):
    p = tmp_path / "plan-cdn.html"
    p.write_text('<html><head>\n<meta name="plangraph-build" content="abc1234567">'
                 "</head></html>", encoding="utf-8")
    assert page_stamp(str(p)) == "abc1234567"
    p.write_text("<html><head><meta name='other' content='x'></head></html>",
                 encoding="utf-8")
    assert page_stamp(str(p)) is None
    assert page_stamp(str(tmp_path / "missing.html")) is None


# ---------------------------------------------------------------------------
# 13. check: statuses, one alert per green sha, stderr fallback

def test_check_status_machine_and_single_alert(tmp_path, capsys):
    cfg = make_cfg(tmp_path, alert_cmd=["notify-send"])
    fake = FakeRunner()
    fake.script(("notify-send",), (0, ""))

    assert check(cfg, fake, now=T0)["status"] == "no_green_known"

    write_state(cfg, HEAD, T0)
    write_page(cfg, HEAD[:7])
    assert check(cfg, fake, now=T0 + 10)["status"] == "ok"
    assert fake.calls_where("notify-send") == []

    write_page(cfg, BAD_STAMP)
    r = check(cfg, fake, now=T0 + 600)             # within 1800s grace
    assert r["status"] == "stale"
    assert r["green"] == HEAD
    assert r["page"] == BAD_STAMP
    assert fake.calls_where("notify-send") == []

    r = check(cfg, fake, now=T0 + 3600)            # past grace
    assert r["status"] == "alerted"
    alerts = fake.calls_where("notify-send")
    assert len(alerts) == 1
    assert alerts[0]["argv"] == ["notify-send",
                                 expected_alert_line(BAD_STAMP, HEAD, 60)]
    assert load_state(cfg)["alerted"] == HEAD

    r = check(cfg, fake, now=T0 + 7200)            # same sha again
    assert r["status"] == "stale"
    assert len(fake.calls_where("notify-send")) == 1

    record_green(cfg, HEAD2, now=T0 + 7200)        # new green resets alerted
    r = check(cfg, fake, now=T0 + 7200 + 3600)
    assert r["status"] == "alerted"
    alerts = fake.calls_where("notify-send")
    assert len(alerts) == 2
    assert alerts[1]["argv"] == ["notify-send",
                                 expected_alert_line(BAD_STAMP, HEAD2, 60)]


def test_check_without_alert_cmd_prints_line_to_stderr(tmp_path, capsys):
    cfg = make_cfg(tmp_path)                      # alert_cmd None
    fake = FakeRunner()
    write_state(cfg, HEAD, T0)                    # no page file at all
    res = check(cfg, fake, now=T0 + 3600)
    assert res["status"] == "alerted"
    assert res["page"] is None
    err = capsys.readouterr().err
    assert expected_alert_line(None, HEAD, 60) in err
    assert fake.calls_where("notify-send") == []


# ---------------------------------------------------------------------------
# 14. end-to-end: follow moves, then stamp check on the rendered page

def test_follow_then_page_stamp_check_end_to_end(tmp_path):
    cfg = make_cfg(tmp_path, alert_cmd=["pager"])
    put_token(tmp_path)
    fake = FakeRunner()
    fake.script(("pager",), (0, ""))
    t = cfg.tree
    fake.script(("git", "-C", t, "fetch"), (0, ""))
    fake.script(("git", "-C", t, "rev-parse", "origin/main"), (0, STAMP_SHA + "\n"))
    fake.script(("git", "-C", t, "rev-parse", "HEAD"), (0, CUR + "\n"))
    fake.script(("git", "-C", t, "merge-base", "--is-ancestor"), (0, ""))
    fake.script(("git", "-C", t, "merge", "--ff-only"), (0, ""))
    fake.script(("gh", "api", pulls_endpoint(STAMP_SHA)), gh_json([]))
    fake.script(("gh", "api", runs_endpoint(STAMP_SHA)),
                gh_json(runs_payload(run_entry())))

    write_page(cfg, "abc1234567")                 # rendered with the matching stamp
    res = follow(cfg, fake, now=T0)
    assert res["action"] == "moved"
    assert res["sha"].startswith("abc1234567")
    assert check(cfg, fake, now=T0)["status"] == "ok"

    write_page(cfg, "1111111111")                 # page rolled back to an old build
    assert check(cfg, fake, now=T0 + 60)["status"] == "stale"
    assert fake.calls_where("pager") == []
    r = check(cfg, fake, now=T0 + 1800 + 120)
    assert r["status"] == "alerted"
    alerts = fake.calls_where("pager")
    assert len(alerts) == 1
    assert alerts[0]["argv"] == ["pager", expected_alert_line("1111111111", STAMP_SHA, 32)]
    assert check(cfg, fake, now=T0 + 999999)["status"] == "stale"


# ---------------------------------------------------------------------------
# 15. main()

def test_main_follow_prints_one_json_line_and_returns_zero(tmp_path, capsys):
    cfg_env = base_env(tmp_path)                  # token file absent -> kept
    fake = FakeRunner()
    rc = main(["follow"], env=cfg_env, runner=fake, now=T0)
    out = capsys.readouterr().out
    assert rc == 0
    lines = out.strip().splitlines()
    assert len(lines) == 1
    doc = json.loads(lines[0])
    assert doc["action"] == "kept"


@pytest.mark.parametrize("missing", [
    "PLANGRAPH_LIVE_TREE", "PLANGRAPH_REPO",
    "PLANGRAPH_GH_TOKEN_FILE", "PLANGRAPH_OUT",
])
def test_main_missing_env_var_returns_two(tmp_path, missing, capsys):
    env = base_env(tmp_path, **{missing: None})
    rc = main(["follow"], env=env, runner=FakeRunner(), now=T0)
    assert rc == 2
    err = capsys.readouterr().err
    line = [l for l in err.splitlines() if l.startswith("live_follow: ")]
    assert line, err
    assert missing in line[0]


def test_main_output_never_contains_token(tmp_path, capsys):
    env = base_env(tmp_path)
    put_token(tmp_path)
    fake = FakeRunner()
    cfg = Config.from_env(env)
    t = cfg.tree
    # head == cur -> follow consults CI on the up-to-date path; gh fails (rc 1)
    fake.script(("git", "-C", t, "fetch"), (0, ""))
    fake.script(("git", "-C", t, "rev-parse", "origin/main"), (0, CUR + "\n"))
    fake.script(("git", "-C", t, "rev-parse", "HEAD"), (0, CUR + "\n"))
    fake.script(("gh", "api", pulls_endpoint(CUR)), (1, ""))
    rc = main(["follow"], env=env, runner=fake, now=T0)
    cap = capsys.readouterr()
    assert rc == 0
    assert SENTINEL not in cap.out
    assert SENTINEL not in cap.err
    assert len(cap.out.strip().splitlines()) == 1
    assert json.loads(cap.out.strip())["action"] == "kept"


# ---------------------------------------------------------------------------
# 16. Config.from_env

def test_config_from_env_defaults_and_required(tmp_path):
    env = base_env(tmp_path, PLANGRAPH_STATE=None)  # None drops the key: exercise the default
    cfg = Config.from_env(env)
    assert cfg.tree == env["PLANGRAPH_LIVE_TREE"]
    assert cfg.repo == REPO
    assert cfg.token_file == env["PLANGRAPH_GH_TOKEN_FILE"]
    assert cfg.out == env["PLANGRAPH_OUT"]
    assert Path(cfg.state) == Path(cfg.out) / ".live-follow.json"
    assert cfg.workflow == "tests"
    assert cfg.remote == "origin"
    assert cfg.branch == "main"
    assert cfg.alert_cmd is None
    assert cfg.grace_s == 1800

    env2 = base_env(tmp_path, PLANGRAPH_STATE=str(tmp_path / "st.json"),
                    PLANGRAPH_CI_WORKFLOW="ci",
                    PLANGRAPH_ALERT_CMD="alert --dry-run 'x y'",
                    PLANGRAPH_ALERT_GRACE_S="60")
    cfg2 = Config.from_env(env2)
    assert cfg2.state == env2["PLANGRAPH_STATE"]
    assert cfg2.workflow == "ci"
    assert cfg2.alert_cmd == ["alert", "--dry-run", "x y"]
    assert cfg2.grace_s == 60


@pytest.mark.parametrize("missing", [
    "PLANGRAPH_LIVE_TREE", "PLANGRAPH_REPO",
    "PLANGRAPH_GH_TOKEN_FILE", "PLANGRAPH_OUT",
])
def test_config_from_env_required_names_variable(tmp_path, missing):
    env = base_env(tmp_path, **{missing: None})
    with pytest.raises(ConfigError) as ei:
        Config.from_env(env)
    assert isinstance(ei.value, ValueError)
    assert missing in str(ei.value)
