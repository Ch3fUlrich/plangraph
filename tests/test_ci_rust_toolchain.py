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


def assert_no_toolchain_environment(owner, name):
    env = owner.get("env", {})
    assert isinstance(env, dict) and "RUSTUP_TOOLCHAIN" not in env, name


def assert_steps_use_pin(steps, name, shared_installer=False):
    installs = False
    for step in steps:
        assert_no_toolchain_environment(step, name)
        uses = step.get("uses", "")
        if re.search(r"rust-toolchain|setup-rust|rustup", uses, re.I):
            assert (uses == SETUP or shared_installer and re.fullmatch(
                r"dtolnay/rust-toolchain@[0-9a-f]{40}", uses)), name
        assert not re.search(
            r"\brustup\s+(?:toolchain|install|update|default|override)\b"
            r"|\bcargo\s+\+\S+|\bRUSTUP_TOOLCHAIN\b", step.get("run", "")), name
        if uses == SETUP:
            assert set(step.get("with", {})) <= {"components", "targets"}, name
            installs = True
    return installs


def assert_workflow_uses_pin(workflow):
    assert_no_toolchain_environment(workflow, "workflow")
    installers = set()
    for name, job in workflow["jobs"].items():
        assert_no_toolchain_environment(job, name)
        if assert_steps_use_pin(job.get("steps", []), name):
            installers.add(name)
    return installers


def test_ci_rust_installers_read_shared_pin():
    installers = {}
    for path in sorted((ROOT / ".github/workflows").rglob("*")):
        if path.suffix in {".yml", ".yaml"}:
            installers[path.name] = assert_workflow_uses_pin(yaml.safe_load(path.read_text()))
    assert installers["tests.yml"] == {
        "pytest", "pytest-postgres", "rust", "rust-windows", "rust-worker", "rust-spike",
    }
    for path in sorted((ROOT / ".github/actions").rglob("*")):
        if path.name not in {"action.yml", "action.yaml"}:
            continue
        action = yaml.safe_load(path.read_text())
        name = path.relative_to(ROOT).as_posix()
        assert_no_toolchain_environment(action, name)
        assert_steps_use_pin(action["runs"].get("steps", []), name,
                             shared_installer=path.parent == ROOT / SETUP)


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
    assert re.fullmatch(r"dtolnay/rust-toolchain@[0-9a-f]{40}", install["uses"])
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
    {"run": "rustup override set nightly"},
    {"run": "rustup toolchain install 1.0.0"},
    {"run": "cargo +nightly clippy --all-targets -- -D warnings"},
    {"run": "cargo +1.0.0 test"},
    {"run": "cargo\t+stable build"},
    {"env": {"RUSTUP_TOOLCHAIN": "nightly"}, "run": "cargo test"},
    {"run": "RUSTUP_TOOLCHAIN=nightly cargo test"},
])
def test_workflow_cannot_bypass_shared_installer(step):
    with pytest.raises(AssertionError, match="rust"):
        assert_workflow_uses_pin({"jobs": {"rust": {"steps": [step]}}})


@pytest.mark.parametrize("scope", ["workflow", "job"])
@pytest.mark.parametrize("toolchain", ["stable", "nightly", "1.0.0"])
def test_workflow_cannot_override_toolchain_in_environment(scope, toolchain):
    workflow = {"jobs": {"rust": {"steps": [{"uses": SETUP}]}}}
    owner = workflow if scope == "workflow" else workflow["jobs"]["rust"]
    owner["env"] = {"RUSTUP_TOOLCHAIN": toolchain}
    with pytest.raises(AssertionError, match=scope if scope == "workflow" else "rust"):
        assert_workflow_uses_pin(workflow)


@pytest.mark.parametrize("path", [
    ".github/workflows/extra.yml",
    ".github/workflows/extra.yaml",
    ".github/actions/extra/action.yml",
    ".github/actions/extra/action.yaml",
])
@pytest.mark.parametrize("step", [
    {"uses": "dtolnay/rust-toolchain@stable"},
    {"uses": "dtolnay/rust-toolchain@" + "a" * 40},
    {"run": "rustup toolchain install nightly"},
    {"run": "rustup default nightly"},
    {"run": "rustup override set nightly"},
    {"run": "cargo +nightly test"},
    {"env": {"RUSTUP_TOOLCHAIN": "nightly"}, "run": "cargo test"},
])
def test_every_ci_file_rejects_toolchain_bypasses(tmp_path, monkeypatch, path, step):
    # Keep the real CI configuration; inject an installer outside tests.yml/setup-rust.
    for source in (ROOT / ".github").rglob("*.y*ml"):
        target = tmp_path / source.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text())
    extra = tmp_path / path
    extra.parent.mkdir(parents=True, exist_ok=True)
    content = ({"jobs": {"rust": {"steps": [step]}}} if "workflows" in extra.parts
               else {"runs": {"using": "composite", "steps": [step]}})
    extra.write_text(yaml.safe_dump(content))
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    with pytest.raises(AssertionError):
        test_ci_rust_installers_read_shared_pin()


def test_workflow_allows_shared_installer_and_normal_cargo_commands():
    workflow = {"env": {"CARGO_BUILD_JOBS": "2"}, "jobs": {"rust": {
        "env": {"RUST_BACKTRACE": "1"}, "steps": [
            {"uses": SETUP, "with": {"components": "rustfmt, clippy", "targets": "target"}},
            {"run": "cargo fmt --check\ncargo clippy --all-targets -- -D warnings\ncargo test"},
        ],
    }}}
    assert assert_workflow_uses_pin(workflow) == {"rust"}


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
