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


def state(root, base, head):
    return {"worktree": str(root), "branch": "batch/t", "base_head": base, "head": head, "name": "t"}


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


def test_a_train_that_does_not_touch_rust_runs_no_windows_job(tmp_path, monkeypatch):
    root = tmp_path / "wt"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "a.md").write_text("a")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD")
    (root / "a.md").write_text("b")
    git(root, "commit", "-qam", "docs")
    monkeypatch.setenv("PLANGRAPH_WINDOWS_HOST", "ws-build")
    transport = Transport()
    assert land.windows_gate(state(root, base, git(root, "rev-parse", "HEAD")), WORKFLOW, transport=transport) is None
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
