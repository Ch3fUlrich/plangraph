"""Hermetic command recording and real merge-conflict coverage."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


SPEC = importlib.util.spec_from_file_location(
    "stacked_pr_ci", Path(__file__).parents[1] / "tools/stacked_pr_ci.py")
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)
CALLS = []
OLD = "2020-01-01T00:00:00Z"


def result(value="", code=0):
    return subprocess.CompletedProcess([], code, value, "failed" if code else "")


class Fake:
    def __init__(self):
        self.calls = []
        self.prs = []
        self.base = "stack"
        self.merged = True
        self.branch = "topic"
        self.dirty = ""
        self.heads = iter(["a" * 40, "b" * 40])
        self.counts = iter([1] * 30)
        self.date = OLD
        self.fail = None
        self.delivery = False

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        CALLS.append(argv)
        text = " ".join(argv)
        if self.fail and self.fail in text:
            return result(code=1)
        if argv[0] == "notify":
            return result(code=int(self.delivery))
        if argv[:3] == ["gh", "pr", "view"]:
            return result(json.dumps(dict(baseRefName=self.base, headRefName="topic")))
        if argv[:3] == ["gh", "pr", "list"]:
            rows = ([dict(headRefName="stack")] if self.merged else [])
            return result(json.dumps(rows if "merged" in argv else self.prs))
        if argv[:2] == ["gh", "api"]:
            value = (dict(total_count=next(self.counts)) if text.endswith("check-runs")
                     else {"commit": {"committer": {"date": self.date}}})
            return result(json.dumps(value))
        if argv[0] == "git":
            if argv[1] == "branch":
                return result(self.branch)
            if argv[1] == "status":
                return result(self.dirty)
            if argv[1] == "rev-parse":
                return result(next(self.heads))
        return result()


def invoke(fake, command="retarget", *extra):
    args = [command, "--repo", "owner/project"]
    if command == "retarget":
        args += ["--pr", "7", "--checkout", ".", "--wait-s", "0"]
    return ci.main(args + list(extra), runner=fake)


def pr(number=7, base="main", mergeable="MERGEABLE", head="a" * 40):
    return dict(number=number, baseRefName=base, mergeable=mergeable,
                headRefOid=head, createdAt=OLD)


def writes(fake):
    return [a[2] for a, _ in fake.calls if a[:2] == ["gh", "pr"]
            and a[2] in ("edit", "close", "reopen")]


@pytest.mark.parametrize("counts,expected,code", [([1], ["edit"], 0),
    ([0, 1], ["edit", "close", "reopen"], 0),
    ([0, 0], ["edit", "close", "reopen"], 4)])
def test_retarget(counts, expected, code, capsys):
    fake = Fake()
    fake.counts = iter(counts)
    assert invoke(fake) == code
    assert writes(fake) == expected
    calls = [a for a, _ in fake.calls]
    assert ["git", "fetch", "origin", "main"] in calls
    assert ["git", "merge", "--no-edit", "origin/main"] in calls
    assert ["git", "push", "origin", "HEAD:topic"] in calls
    assert ("RETARGETED #7 bbbbbbbbbbbb 1" if code == 0 else "no_check_run") in capsys.readouterr().out


@pytest.mark.parametrize("attribute,value", [("merged", False), ("dirty", " M file"),
                                           ("branch", "other")])
def test_refusals(attribute, value):
    fake = Fake()
    setattr(fake, attribute, value)
    assert invoke(fake) == 2
    assert not writes(fake)
    assert not any(a[1] in ("fetch", "merge", "push") for a, _ in fake.calls if a[0] == "git")


def test_main_unchanged():
    fake = Fake()
    fake.base = "main"
    fake.heads = iter(["a" * 40] * 2)
    assert invoke(fake) == 0
    assert not writes(fake)
    assert not any(a[1] == "push" for a, _ in fake.calls if a[0] == "git")


def test_real_conflict(tmp_path, capsys):
    def git(*args):
        CALLS.append(["git", *args])
        return subprocess.run(["git", *args], cwd=tmp_path, text=True,
                              capture_output=True, check=True).stdout.strip()
    git("init", "-b", "main")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    file = tmp_path / "CHANGELOG"
    file.write_text("base\n")
    git("add", ".")
    git("commit", "-m", "base")
    git("checkout", "-b", "topic")
    file.write_text("topic\n")
    git("commit", "-am", "topic")
    git("checkout", "main")
    file.write_text("main\n")
    git("commit", "-am", "main")
    git("remote", "add", "origin", str(tmp_path))
    git("checkout", "topic")
    fake = Fake()
    def runner(argv, **kwargs):
        if argv[0] == "gh":
            return fake(argv, **kwargs)
        CALLS.append(argv)
        return subprocess.run(argv, text=True, capture_output=True, **kwargs)
    assert ci.main(["retarget", "--repo", "owner/project", "--pr", "7",
                    "--checkout", str(tmp_path), "--wait-s", "0"], runner=runner) == 3
    assert "conflicting CHANGELOG" in capsys.readouterr().out
    assert git("status", "--porcelain") == ""
    assert file.read_text() == "topic\n"
    assert ["git", "merge", "--abort"] in CALLS
    assert not any(a[1] == "push" for a in CALLS[-12:] if a[0] == "git")


@pytest.mark.parametrize("cause", ["conflicting", "no_check_run", "base_merged"])
def test_dedup(cause, tmp_path, capsys):
    fake = Fake()
    fake.prs = [pr(base="stack" if cause == "base_merged" else "main",
                   mergeable="CONFLICTING" if cause == "conflicting" else "MERGEABLE")]
    fake.counts = iter([0 if cause == "no_check_run" else 1] * 10)
    state = tmp_path / "state"
    def check():
        return invoke(fake, "check", "--state", str(state))
    assert check() == 1
    assert cause in capsys.readouterr().out
    assert check() == 0
    assert not capsys.readouterr().out
    fake.prs[0]["headRefOid"] = "b" * 40
    assert check() == 1
    assert "bbbbbbbbbbbb" in capsys.readouterr().out
    assert state.stat().st_mode & 0o777 == 0o600
    fake.prs = []
    assert check() == 0
    assert json.loads(state.read_text()) == []
    assert not writes(fake)


@pytest.mark.parametrize("kind", ["clean", "open_stack", "young", "unknown"])
def test_no_finding(kind, tmp_path, capsys):
    fake = Fake()
    fake.prs = [pr(base="stack" if kind == "open_stack" else "main",
                   mergeable="UNKNOWN" if kind == "unknown" else "MERGEABLE")]
    fake.merged = False
    if kind == "young":
        fake.counts = iter([0])
        fake.date = "2999-01-01T00:00:00Z"
    assert invoke(fake, "check", "--state", str(tmp_path / "state")) == 0
    assert not capsys.readouterr().out
    assert not writes(fake)


@pytest.mark.parametrize("failure", ["pr list", "check-runs", "commits/", "--state merged"])
def test_read_failure(failure, tmp_path):
    state = tmp_path / "state"
    state.write_text('[[99, "old", "conflicting"]]')
    before = state.read_bytes()
    fake = Fake()
    fake.prs = [pr(base="stack" if failure == "--state merged" else "main")]
    fake.counts = iter([0])
    fake.fail = failure
    assert invoke(fake, "check", "--state", str(state)) == 2
    assert state.read_bytes() == before


def test_delivery_retry(tmp_path, capsys):
    fake = Fake()
    fake.prs = [pr(mergeable="CONFLICTING")]
    fake.delivery = True
    for _ in range(2):
        assert invoke(fake, "check", "--state", str(tmp_path / "state"),
                      "--alert-cmd", 'notify "two words"') == 1
        output = capsys.readouterr()
        assert "conflicting" in output.out
        assert "alert delivery failed" in output.err
    deliveries = [(a, kw) for a, kw in fake.calls if a[0] == "notify"]
    assert len(deliveries) == 2
    assert deliveries[0][0] == ["notify", "two words"]
    assert deliveries[0][1]["input"].startswith("ALERT #7 ")
    assert json.loads((tmp_path / "state").read_text()) == []


@pytest.fixture(autouse=True)
def no_dangerous_calls():
    yield
    for argv in CALLS:
        assert "--force" not in argv
        assert "rebase" not in argv
        assert not (argv[:2] == ["git", "push"] and "-f" in argv)
