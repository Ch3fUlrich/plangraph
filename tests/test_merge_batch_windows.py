"""A train's merged head runs the CI Windows selection on ws-build before it is pushed (LAND-TRAIN-WINDOWS-TESTS-BEFORE-PUSH)."""
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import merge_batch_land as land  # noqa: E402
from merge_batch_util import MergeBatchError  # noqa: E402

WORKFLOW = (TOOLS.parent / ".github/workflows/tests.yml").read_text(encoding="utf-8")
HEAD = "a" * 40


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.invalid", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def train(tmp_path):
    """A worktree whose batch branch changed a Rust file against its base."""
    root = tmp_path / "wt"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "rust/pg-spawnd/src").mkdir(parents=True)
    (root / "rust/pg-spawnd/src/lib.rs").write_text("")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "-b", "batch/t")
    (root / "rust/pg-spawnd/src/lib.rs").write_text("// change\n")
    git(root, "commit", "-qam", "part")
    return root, base, git(root, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def no_state_file(monkeypatch):
    monkeypatch.setattr(land, "save_state", lambda st: None)


def state(root, base, head, branch="batch/t"):
    return {"worktree": str(root), "branch": branch, "base": "main", "base_head": base, "head": head, "name": "t"}


def test_the_windows_selection_is_the_one_the_ci_windows_job_runs():
    assert land.windows_commands(WORKFLOW) == [["cargo", "test", "-p", "pg-spawnd"],
                                               ["cargo", "test", "-p", "pg-worker-core"]]


def test_a_workflow_without_the_windows_job_is_an_error():
    with pytest.raises(MergeBatchError, match="no Windows job"):
        land.windows_commands("jobs:\n  rust:\n    steps: []\n")


@pytest.mark.parametrize("paths,needed", [
    (["rust/pg-spawnd/src/lib.rs"], True), (["src/plangraph/planner/provider_stop.py"], True),
    (["docs/x.md"], False), (["tests/test_x.py"], False)])
def test_windows_is_needed_exactly_when_ci_selects_rust(paths, needed):
    assert land.windows_needed(paths) is needed


def test_the_remote_script_clones_the_bundle_runs_every_command_and_always_cleans_up():
    script = land.windows_script("train.bundle", HEAD, [["cargo", "test", "-p", "pg-spawnd"]], 20)
    assert HEAD in script and "train.bundle" in script
    assert "cargo test -p pg-spawnd --no-fail-fast" in script
    assert "CARGO_TARGET_DIR" in script and "finally" in script and "Remove-Item" in script
    assert "20" in script   # the target cap


class Transport:
    def __init__(self, rc=0, out=""):
        self.rc, self.out, self.calls = rc, out, []

    def __call__(self, argv, input_text=None, timeout=None):
        self.calls.append((argv, input_text))
        if argv[0] == "scp":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, self.rc, self.out, "")


def test_a_windows_only_red_refuses_the_push_and_names_the_test_ids(train, monkeypatch):
    root, base, head = train
    monkeypatch.setenv("PLANGRAPH_WINDOWS_HOST", "ws-build")
    transport = Transport(1, "test quota::reads_userprofile ... FAILED\ntest other::fine ... ok\n")
    with pytest.raises(MergeBatchError) as exc:
        land.windows_gate(state(root, base, head), WORKFLOW, transport=transport)
    assert "quota::reads_userprofile" in str(exc.value) and "ws-build" in str(exc.value)
    assert any("ssh" in call[0][0] for call in transport.calls)


def test_a_green_windows_run_is_recorded_with_its_duration(train, monkeypatch):
    root, base, head = train
    monkeypatch.setenv("PLANGRAPH_WINDOWS_HOST", "ws-build")
    st = state(root, base, head)
    line = land.windows_gate(st, WORKFLOW, transport=Transport(0, "test result: ok\n"))
    assert line.startswith(f"windows ok head={head[:12]} host=ws-build commands=2 seconds=")
    assert st["windows"]["head"] == head and st["windows"]["line"] == line
    assert land.windows_report(st, head) == line


def docs_only(tmp_path, branch):
    root = tmp_path / "wt"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a.md").write_text("a")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "-b", branch)
    (root / "a.md").write_text("b")
    git(root, "commit", "-qam", "docs")
    return root, base, git(root, "rev-parse", "HEAD")


def test_a_non_candidate_that_selects_no_rust_runs_no_windows_job(tmp_path, monkeypatch):
    root, base, head = docs_only(tmp_path, "lane/docs")
    monkeypatch.setenv("PLANGRAPH_WINDOWS_HOST", "ws-build")
    transport = Transport()
    assert land.windows_gate(state(root, base, head, "lane/docs"), WORKFLOW, transport=transport) is None
    assert transport.calls == []


def test_a_docs_only_batch_candidate_still_runs_windows_because_ci_runs_the_full_suite(tmp_path, monkeypatch):
    root, base, head = docs_only(tmp_path, "batch/docs")
    monkeypatch.setenv("PLANGRAPH_WINDOWS_HOST", "ws-build")
    transport = Transport(0, "test result: ok\n")
    line = land.windows_gate(state(root, base, head, "batch/docs"), WORKFLOW, transport=transport)
    assert line and line.startswith("windows ok")
    assert transport.calls


def test_a_block_scalar_run_step_is_read_and_an_empty_selection_is_refused():
    workflow = ("jobs:\n  rust-windows:\n    steps:\n      - name: t\n        run: |\n"
                "          cargo test -p pg-spawnd\n          cargo test -p pg-worker-core --lib\n"
                "  next:\n    steps: []\n")
    assert land.windows_commands(workflow) == [["cargo", "test", "-p", "pg-spawnd"],
                                               ["cargo", "test", "-p", "pg-worker-core", "--lib"]]
    with pytest.raises(MergeBatchError, match="zero tests"):
        land.windows_commands("jobs:\n  rust-windows:\n    steps:\n      - run: echo hi\n  other:\n    steps: []\n")


JOB = {"name": "pytest", "created_at": "2026-11-21T10:00:00Z", "started_at": "2026-11-21T10:01:00Z",
       "completed_at": "2026-11-21T10:11:00Z", "labels": ["self-hosted"]}


def test_the_part_cost_is_recorded_per_part_from_its_own_run_and_for_the_candidate(tmp_path, monkeypatch):
    part1, part2, cand = "1" * 40, "2" * 40, HEAD

    def api(*args, **kwargs):
        endpoint = args[1]
        if "workflows/tests.yml/runs" in endpoint:
            sha = endpoint.split("head_sha=")[1].split("&")[0]
            return {"workflow_runs": [{"id": {part1: 11, part2: 22}[sha], "head_sha": sha, "status": "completed"}]}
        return {"jobs": [JOB]}

    monkeypatch.setattr(land, "gh_json", api)
    st = {"repo": "o/p", "pr": 90, "merged": [{"pr": 11, "head": part1}, {"pr": 22, "head": part2}]}
    rows = land.record_part_cost(st, 99, cand, telemetry_dir=tmp_path)
    assert [(r["kind"], r["pr"], r["head"]) for r in rows] == [
        ("part", 11, part1), ("part", 22, part2), ("candidate", 90, cand)]
    assert (tmp_path / "part-cost.jsonl").read_text().count("\n") == 3


def test_a_failing_cost_lookup_never_fails_the_push(tmp_path, monkeypatch, capsys):
    def broken(*a, **k):
        raise MergeBatchError("gh api failed")
    monkeypatch.setattr(land, "gh_json", broken)
    assert land.record_part_cost({"repo": "o/p", "merged": [{"pr": 1, "head": HEAD}]}, 1, HEAD,
                                 telemetry_dir=tmp_path) == []
    assert "not recorded" in capsys.readouterr().err


def test_a_folded_run_step_is_one_command_line():
    workflow = ("jobs:\n  rust-windows:\n    steps:\n      - run: >\n          cargo test -p pg-spawnd\n"
                "          --lib\n  next:\n    steps: []\n")
    assert land.windows_commands(workflow) == [["cargo", "test", "-p", "pg-spawnd", "--lib"]]


def test_the_pull_requests_own_labels_decide_the_full_run(tmp_path, monkeypatch):
    """A docs-only batch/* pull request labelled land-part is a part: CI selects no Windows job, so neither does the gate."""
    root, base, head = docs_only(tmp_path, "batch/docs")
    monkeypatch.setenv("PLANGRAPH_WINDOWS_HOST", "ws-build")
    monkeypatch.setattr(land, "gh_json", lambda *a, **k: {"labels": [{"name": "land-part"}]})
    transport = Transport()
    st = state(root, base, head, "batch/docs")
    st.update(pr=5, repo="o/p")
    assert land.windows_gate(st, WORKFLOW, transport=transport) is None
    assert transport.calls == []


def test_without_a_configured_host_the_gate_says_so_and_the_go_req_has_no_windows_part(train, monkeypatch, capsys):
    root, base, head = train
    monkeypatch.delenv("PLANGRAPH_WINDOWS_HOST", raising=False)
    st = state(root, base, head)
    assert land.windows_gate(st, WORKFLOW, transport=Transport()) is None
    assert "PLANGRAPH_WINDOWS_HOST" in capsys.readouterr().err
    assert land.windows_report(st, head) is None


def test_a_result_for_another_head_is_not_reported(train):
    root, base, head = train
    st = state(root, base, head)
    st["windows"] = {"head": "b" * 40, "line": "windows ok head=bbbbbbbbbbbb host=h commands=2 seconds=1"}
    assert land.windows_report(st, head) is None


def test_push_adopts_an_existing_part_before_selecting_windows(tmp_path, monkeypatch):
    from types import SimpleNamespace

    root, base, head = docs_only(tmp_path, "batch/docs")
    workflow = root / ".github/workflows/tests.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(WORKFLOW)
    st = state(root, base, head, "batch/docs")
    st.update(status="built", repo="o/p", merged=[{"pr": 1}])
    monkeypatch.setattr(land, "load_state", lambda _: st)
    real_git = land.git
    monkeypatch.setattr(land, "git", lambda *a, **k: SimpleNamespace(stdout="") if a[0] == "push" else real_git(*a, **k))
    monkeypatch.setattr(land, "adopt_open_pr", lambda current: current.update(pr=5, url="https://example.invalid/pull/5"))
    monkeypatch.setattr(land, "gh_json", lambda *a, **k: {"state": "OPEN", "labels": [{"name": "land-part"}]})
    monkeypatch.setattr(land, "render_body", lambda _: "")
    monkeypatch.setattr(land, "update_pr", lambda *a: None)
    monkeypatch.setenv("PLANGRAPH_WINDOWS_HOST", "fixture")
    transport = Transport()
    gate = land.windows_gate
    monkeypatch.setattr(land, "windows_gate", lambda st, workflow: gate(st, workflow, transport=transport))

    assert land.cmd_push(SimpleNamespace(name="t", ci_run=None)) == 0
    assert not transport.calls
