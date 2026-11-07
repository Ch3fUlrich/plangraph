import importlib.util
from pathlib import Path
import subprocess

SPEC = importlib.util.spec_from_file_location(
    "dell_ci_run", Path(__file__).parents[1] / "tools/dell_ci_run.py")
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


def test_protoc_path_and_missing_reason(tmp_path):
    script = ci.service_script("rust-spike", str(tmp_path / "scratch"))
    assert 'export PATH="$HOME/.local/bin:$PATH"' in script
    script = script.replace("command -v protoc", "command -v nonexistent_protoc_test")
    result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
    assert result.returncode == 1
    assert "rust-spike: host fault: protoc missing" in result.stderr


def test_failure_highlights_precede_tail():
    # No replacement exists after 9170e867 removed output_tail. Keep this
    # contract visible until the intended diagnostics policy is resolved.
    output = "test core::broken ... FAILED\nthread 'core::broken' panicked at src/lib.rs:1\n"
    tail = ci.output_tail(output + "noise\n" * 4000)
    assert tail.startswith("test core::broken ... FAILED\nthread 'core::broken' panicked")
    assert len(tail) <= ci.OUTPUT_TAIL_LIMIT


def test_empty_output_names_command(tmp_path, monkeypatch):
    # Exercise the remote producer: even a silent failing cargo command must
    # identify itself before the service script exits and cleans up.
    monkeypatch.setitem(ci.service_script.__globals__["RUST"], "rust-spike",
                        "cargo test")
    script = ci.service_script("rust-spike", str(tmp_path / "scratch"))
    stubs = "protoc() { :; }; cargo() { return 7; }; "
    result = subprocess.run(["sh", "-c", stubs + script],
                            capture_output=True, text=True)
    assert result.returncode == 7
    assert result.stdout == ""
    assert result.stderr == "Running: cargo test\n"
    assert not (tmp_path / "scratch").exists()
