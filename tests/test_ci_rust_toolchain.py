"""Local Rust builds and every CI installer share one release pin (D-5297)."""
from pathlib import Path
import re
import subprocess
import tomllib

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SETUP = "./.github/actions/setup-rust"
PIN = "rust/rust-toolchain.toml"


def assert_workflow_uses_pin(workflow):
    installers = set()
    for name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            uses = step.get("uses", "")
            if re.search(r"rust-toolchain|setup-rust|rustup", uses):
                assert uses == SETUP, name
            assert not re.search(r"rustup\s+(?:toolchain|install|update|default|override)",
                                 step.get("run", "")), name
            if uses == SETUP:
                assert set(step.get("with", {})) <= {"components", "targets"}, name
                installers.add(name)
    return installers


def test_ci_rust_installers_read_shared_pin():
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    assert assert_workflow_uses_pin(workflow) == {
        "pytest", "pytest-postgres", "rust", "rust-windows", "rust-worker", "rust-spike",
    }


def test_local_rust_toolchain_is_an_exact_release():
    toolchain = tomllib.loads((ROOT / PIN).read_text())["toolchain"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", toolchain["channel"])
    assert toolchain["profile"] == "minimal"
    assert set(toolchain["components"]) == {"rustfmt", "clippy"}
    assert not (ROOT / "rust-toolchain.toml").exists()
    assert not (ROOT / "rust-toolchain").exists()
    assert list((ROOT / "rust").rglob("rust-toolchain.toml")) == [ROOT / PIN]
    assert not list((ROOT / "rust").rglob("rust-toolchain"))


def test_shared_installer_reads_pin_without_a_version_override(tmp_path):
    action = yaml.safe_load((ROOT / ".github/actions/setup-rust/action.yml").read_text())
    assert set(action["inputs"]) == {"components", "targets"}
    parse, install = action["runs"]["steps"]
    assert parse["id"] == "pin"
    assert parse["shell"] == "bash"
    assert parse["working-directory"] == "${{ github.workspace }}"
    assert PIN in parse["run"]
    output = tmp_path / "github-output"
    # Execute the real shell reader, with no rustup/network or inherited environment.
    subprocess.run(["bash", "-euo", "pipefail", "-c", parse["run"]], cwd=ROOT,
                   env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(output)}, check=True)
    channel = tomllib.loads((ROOT / PIN).read_text())["toolchain"]["channel"]
    assert output.read_text() == f"toolchain={channel}\n"
    assert install["uses"] == "dtolnay/rust-toolchain@master"
    assert install["with"] == {
        "toolchain": "${{ steps.pin.outputs.toolchain }}",
        "components": "${{ inputs.components }}",
        "targets": "${{ inputs.targets }}",
    }


@pytest.mark.parametrize("toolchain", ["stable", "nightly", "1.0.0"])
def test_workflow_cannot_install_a_different_toolchain(toolchain):
    workflow = {"jobs": {"rust": {"steps": [{
        "uses": f"dtolnay/rust-toolchain@{toolchain}",
    }]}}}
    with pytest.raises(AssertionError, match="rust"):
        assert_workflow_uses_pin(workflow)


def test_workflow_cannot_override_shared_installer_pin():
    workflow = {"jobs": {"rust": {"steps": [{
        "uses": SETUP, "with": {"toolchain": "stable"},
    }]}}}
    with pytest.raises(AssertionError, match="rust"):
        assert_workflow_uses_pin(workflow)


@pytest.mark.parametrize("step", [
    {"uses": "actions-rust-lang/setup-rust-toolchain@v1"},
    {"run": "rustup toolchain install stable"},
    {"run": "rustup default nightly"},
])
def test_workflow_cannot_bypass_shared_installer(step):
    with pytest.raises(AssertionError, match="rust"):
        assert_workflow_uses_pin({"jobs": {"rust": {"steps": [step]}}})


@pytest.mark.parametrize("channel", ["stable", "nightly", "1.100"])
def test_shared_installer_refuses_unpinned_channels(tmp_path, channel):
    action = yaml.safe_load((ROOT / ".github/actions/setup-rust/action.yml").read_text())
    pin = tmp_path / PIN
    pin.parent.mkdir()
    pin.write_text(f'[toolchain]\nchannel = "{channel}"\n')
    output = tmp_path / "github-output"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", action["runs"]["steps"][0]["run"]],
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(output)})
    assert result.returncode != 0
    assert not output.exists()
