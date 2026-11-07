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
