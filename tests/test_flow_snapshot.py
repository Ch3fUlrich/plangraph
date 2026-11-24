"""The flow-snapshot collector contract: its Rust tests, run by the item's named command."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUITES = (
    ("pg-worker-core", ("--test", "flow_collect")),
    ("plangraph-cli", ("--test", "flow_snapshot")),
    ("plangraph-cli", ("--lib", "flow_snapshot")),
)


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo is not installed")
@pytest.mark.parametrize(("package", "target"), SUITES)
def test_flow_snapshot_rust_suite(package, target):
    result = subprocess.run(
        ["cargo", "test", "--manifest-path", str(ROOT / "rust/Cargo.toml"), "-p", package, *target],
        capture_output=True, text=True, timeout=1800,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
