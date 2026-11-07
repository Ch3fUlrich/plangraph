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
    output = "test core::broken ... FAILED\nthread 'core::broken' panicked at src/lib.rs:1\n"
    tail = ci.output_tail(output + "noise\n" * 4000)
    assert tail.startswith("test core::broken ... FAILED\nthread 'core::broken' panicked")
    assert len(tail) <= ci.OUTPUT_TAIL_LIMIT


def test_empty_output_names_command():
    assert "cargo test" in ci.output_tail("", "", "cargo test")
    assert "Running: cargo fmt --check" in ci.service_script("rust", "/scratch")
