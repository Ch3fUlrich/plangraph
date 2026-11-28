"""Local Rust builds and every CI installer share one release pin (D-5297)."""
from pathlib import Path
import re
import subprocess
import tomllib

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SETUP = "./.github/actions/setup-rust"
SETUP_NEXTEST = "./.github/actions/setup-nextest"
PIN = "rust-toolchain.toml"


def assert_no_toolchain_environment(owner, name):
    env = owner.get("env", {})
    assert isinstance(env, dict) and all(
        str(key).upper() != "RUSTUP_TOOLCHAIN" for key in env), name


def assert_steps_use_pin(steps, name, shared_installer=False):
    installs = False
    for step in steps:
        assert_no_toolchain_environment(step, name)
        uses = step.get("uses", "")
        if re.search(r"rust|toolchain", uses, re.I):
            assert (uses == SETUP or shared_installer and re.fullmatch(
                r"dtolnay/rust-toolchain@[0-9a-f]{40}", uses)), name
        assert not re.search(
            r"\brustup\s+(?:toolchain|install|update|default|override|run)\b"
            r"|\brustup-init\b|\bsh\.rustup\.rs\b|--default-toolchain\b"
            r"|\bcargo\s+\+\S+|\bRUSTUP_TOOLCHAIN\b", step.get("run", ""), re.I), name
        if uses == SETUP:
            assert set(step.get("with", {})) <= {"components", "targets"}, name
            installs = True
    return installs


def assert_workflow_uses_pin(workflow):
    assert_no_toolchain_environment(workflow, "workflow")
    installers = set()
    for name, job in workflow["jobs"].items():
        assert_no_toolchain_environment(job, name)
        container = job.get("container", {})
        if isinstance(container, dict):
            assert_no_toolchain_environment(container, name)
        for service in job.get("services", {}).values():
            assert_no_toolchain_environment(service, name)
        if assert_steps_use_pin(job.get("steps", []), name):
            installers.add(name)
    return installers


def test_ci_rust_installers_read_shared_pin():
    installers = {}
    for path in sorted((ROOT / ".github/workflows").rglob("*")):
        if path.suffix in {".yml", ".yaml"}:
            installers[path.name] = assert_workflow_uses_pin(yaml.safe_load(path.read_text()))
    assert installers["tests.yml"] == {
        "new-code-lang", "pytest", "pytest-postgres", "rust", "rust-test", "rust-windows",
        "rust-worker", "rust-spike",
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
    assert (ROOT / "rust-toolchain.toml").is_file()
    assert not (ROOT / "rust-toolchain").exists()
    assert list((ROOT / "rust").rglob("rust-toolchain.toml")) == []
    assert not list((ROOT / "rust").rglob("rust-toolchain"))


def test_shared_installer_reads_pin_without_a_version_override(tmp_path):
    action = yaml.safe_load((ROOT / ".github/actions/setup-rust/action.yml").read_text())
    assert set(action["inputs"]) == {"components", "targets"}
    parse, install, cache = action["runs"]["steps"]
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
    assert re.search(re.escape(install["uses"]) + r"\s+# v\d+\b", (
        ROOT / ".github/actions/setup-rust/action.yml").read_text())
    assert install["with"] == {
        "toolchain": "${{ steps.pin.outputs.toolchain }}",
        "components": "${{ inputs.components }}",
        "targets": "${{ inputs.targets }}",
    }
    assert cache["working-directory"] == "${{ github.workspace }}"
    assert "--cargo-cache-env \"$GITHUB_ENV\"" in cache["run"]


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
    {"uses": "actions-rs/toolchain@v1"},
    {"uses": "moonrepo/setup-rust@v1"},
    {"uses": "example/toolchain@v1"},
    {"uses": "example/RUST@v1"},
    {"run": "rustup toolchain install stable"},
    {"run": "rustup default nightly"},
    {"run": "rustup override set nightly"},
    {"run": "rustup toolchain install 1.0.0"},
    {"run": "rustup run nightly cargo test"},
    {"run": "rustup\trun\t1.0.0\trustc --version"},
    pytest.param({"run": "./rustup-init -y"}, id="rustup-init"),
    pytest.param({"run": "curl -sSf https://sh.rustup.rs | sh -s -- -y"}, id="rustup-script"),
    pytest.param({"run": "sh installer.sh -y --default-toolchain nightly"}, id="default-toolchain"),
    {"run": "cargo +nightly clippy --all-targets -- -D warnings"},
    {"run": "cargo +1.0.0 test"},
    {"run": "cargo\t+stable build"},
    {"env": {"RUSTUP_TOOLCHAIN": "nightly"}, "run": "cargo test"},
    {"env": {"rustup_toolchain": "nightly"}, "run": "cargo test"},
    {"run": "RUSTUP_TOOLCHAIN=nightly cargo test"},
    {"run": "rustup_toolchain=nightly cargo test"},
])
def test_workflow_cannot_bypass_shared_installer(step):
    with pytest.raises(AssertionError, match="rust"):
        assert_workflow_uses_pin({"jobs": {"rust": {"steps": [step]}}})


@pytest.mark.parametrize("command", ["cargo test", "rustc --version", "rustup show"])
@pytest.mark.parametrize("uses", [
    "actions-rs/toolchain@v1", "moonrepo/setup-rust@v1", "example/RUST@v1",
    "example/toolchain@v1", "dtolnay/rust-toolchain@" + "a" * 40,
])
def test_rust_jobs_allow_only_shared_installer(command, uses):
    steps = [{"uses": SETUP}, {"uses": uses}, {"run": command}]
    with pytest.raises(AssertionError, match="build"):
        assert_workflow_uses_pin({"jobs": {"build": {"steps": steps}}})


@pytest.mark.parametrize("scope", ["workflow", "job", "container", "service"])
@pytest.mark.parametrize("key", ["RUSTUP_TOOLCHAIN", "rustup_toolchain", "Rustup_Toolchain"])
@pytest.mark.parametrize("toolchain", ["stable", "nightly", "1.0.0"])
def test_workflow_cannot_override_toolchain_in_environment(scope, key, toolchain):
    workflow = {"jobs": {"rust": {"steps": [{"uses": SETUP}]}}}
    job = workflow["jobs"]["rust"]
    if scope == "container":
        owner = job.setdefault("container", {"image": "rust"})
    elif scope == "service":
        owner = job.setdefault("services", {}).setdefault("helper", {"image": "rust"})
    else:
        owner = workflow if scope == "workflow" else job
    owner["env"] = {key: toolchain}
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
    {"uses": "actions-rs/toolchain@v1"},
    {"uses": "moonrepo/setup-rust@v1"},
    {"uses": "example/toolchain@v1"},
    {"uses": "example/RUST@v1"},
    {"run": "rustup toolchain install nightly"},
    {"run": "rustup default nightly"},
    {"run": "rustup override set nightly"},
    {"run": "rustup run nightly cargo test"},
    pytest.param({"run": "./rustup-init -y"}, id="rustup-init"),
    pytest.param({"run": "curl -sSf https://sh.rustup.rs | sh -s -- -y"}, id="rustup-script"),
    pytest.param({"run": "sh installer.sh -y --default-toolchain nightly"}, id="default-toolchain"),
    {"run": "cargo +nightly test"},
    {"env": {"RUSTUP_TOOLCHAIN": "nightly"}, "run": "cargo test"},
    {"env": {"Rustup_Toolchain": "nightly"}, "run": "cargo test"},
    {"run": "rustup_toolchain=nightly cargo test"},
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
    with pytest.raises(AssertionError, match=re.escape(
            "rust" if "workflows" in extra.parts else path)):
        test_ci_rust_installers_read_shared_pin()


def test_workflow_allows_shared_installer_and_normal_cargo_commands():
    workflow = {"env": {"CARGO_BUILD_JOBS": "2"}, "jobs": {"rust": {
        "env": {"RUST_BACKTRACE": "1"},
        "container": {"image": "rust", "env": {"RUST_BACKTRACE": "1"}},
        "services": {"helper": {"image": "rust", "env": {"RUST_LOG": "info"}}},
        "steps": [
            {"uses": SETUP, "with": {"components": "rustfmt, clippy", "targets": "target"}},
            {"run": "cargo fmt --check\ncargo clippy --all-targets -- -D warnings\ncargo test"},
        ],
    }}}
    assert assert_workflow_uses_pin(workflow) == {"rust"}
    workflow["jobs"]["rust"]["container"] = "rust"
    assert assert_workflow_uses_pin(workflow) == {"rust"}


@pytest.mark.parametrize("channel", ["stable", "nightly", "1.100"])
def test_shared_installer_refuses_unpinned_channels(tmp_path, channel):
    action = yaml.safe_load((ROOT / ".github/actions/setup-rust/action.yml").read_text())
    pin = tmp_path / PIN
    pin.write_text(f'[toolchain]\nchannel = "{channel}"\n')
    output = tmp_path / "github-output"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", action["runs"]["steps"][0]["run"]],
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(output)})
    assert result.returncode != 0
    assert not output.exists()


SELF_HOSTED_JOBS = {
    "rust": ("rust/Cargo.toml", "rust/target"),
    "rust-worker": ("rust/worker/Cargo.toml", "rust/worker/target"),
    "rust-spike": ("rust/spikes/temporal-spike/Cargo.toml", "rust/spikes/temporal-spike/target"),
}


@pytest.mark.parametrize("job", SELF_HOSTED_JOBS)
def test_self_hosted_rust_jobs_use_local_target_and_skip_cache(job):
    jobs = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())["jobs"]
    spec = jobs[job]
    runs_on = spec["runs-on"]
    gate = spec["env"]["CI_SELF_HOSTED_RUN"]
    boolean = runs_on.removeprefix("${{ ").split(" && fromJSON(")[0]
    assert gate == "${{ (" + boolean + ") && '1' || '0' }}"
    # No job-level default: the overrides must not depend on GITHUB_ENV vs job env precedence.
    assert "CARGO_BUILD_JOBS" not in spec["env"] and "CARGO_TARGET_DIR" not in spec["env"]
    steps = spec["steps"]
    prep = next(s for s in steps if s.get("name") == "Self-hosted cargo build settings")
    assert prep["if"] == "env.CI_SELF_HOSTED_RUN == '1'"
    assert "github.event_name == 'push'" in prep["env"]["CI_CARGO_PERSIST"]
    assert "default_branch" in prep["env"]["CI_CARGO_PERSIST"]
    for line in ('CARGO_BUILD_JOBS=$(nproc)', 'CARGO_TARGET_DIR=$target', "$HOME/cargo-target/$GITHUB_REPOSITORY/", "RUST_TEST_THREADS=8", "$GITHUB_ENV", "Cargo.lock"):
        assert line in prep["run"]
    hosted = next(s for s in steps if s.get("name") == "Hosted cargo build settings")
    assert hosted["if"] == "env.CI_SELF_HOSTED_RUN != '1'"
    assert "CARGO_BUILD_JOBS=4" in hosted["run"]
    caches = [s for s in steps if str(s.get("uses", "")).startswith("actions/cache")]
    assert len(caches) == 1 and caches[0]["if"] == "env.CI_SELF_HOSTED_RUN != '1'"
    assert SELF_HOSTED_JOBS[job][1] in caches[0]["with"]["path"]


def _rust_prep_script():
    jobs = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())["jobs"]
    return next(s for s in jobs["rust"]["steps"]
                if s.get("name") == "Self-hosted cargo build settings")["run"]


def test_self_hosted_target_dir_is_keyed_by_repo_manifest_and_home_and_capped(tmp_path):
    script = _rust_prep_script()
    ws = tmp_path / "ws"
    (ws / "rust").mkdir(parents=True)

    def run(runner, cap="40", persist="1", repo="owner/repo", extra_env=None):
        home = tmp_path / "home" / runner
        home.mkdir(parents=True, exist_ok=True)
        out = tmp_path / f"env-{runner}"
        out.write_text("")
        temp = tmp_path / "runner-temp"
        temp.mkdir(exist_ok=True)
        subprocess.run(["bash", "-c", script], cwd=ws, check=True, env={
            "PATH": "/usr/bin:/bin", "HOME": str(home), "RUNNER_TEMP": str(temp),
            "GITHUB_ENV": str(out), "GITHUB_REPOSITORY": repo,
            "CI_CARGO_PERSIST": persist, "CI_CARGO_TARGET_CAP_GB": cap, **(extra_env or {})})
        return dict(line.split("=", 1) for line in out.read_text().splitlines())

    (ws / "rust/Cargo.toml").write_text("[profile.dev]\ndebug = 0\n")
    first = run("r1")
    assert first["CARGO_BUILD_JOBS"].isdigit() and int(first["CARGO_BUILD_JOBS"]) >= 1
    target = Path(first["CARGO_TARGET_DIR"])
    # The dir is named target: the shards extract the test archive at the same path.
    assert target.name == "target" and target.parent.parent == tmp_path / "home/r1/cargo-target/owner/repo/rust"
    assert first["RUST_TEST_THREADS"] == "8"
    assert not str(target).startswith(str(ws))
    assert run("r2")["CARGO_TARGET_DIR"] != first["CARGO_TARGET_DIR"]
    assert Path(run("r1", repo="other/repo")["CARGO_TARGET_DIR"]) != target
    target.mkdir(parents=True)
    (target / "artifact").write_text("x")
    assert run("r1")["CARGO_TARGET_DIR"] == first["CARGO_TARGET_DIR"]
    assert (target / "artifact").exists()
    # Cargo.lock is part of the key.
    (ws / "rust/Cargo.lock").write_text("a")
    locked = run("r1")["CARGO_TARGET_DIR"]
    assert locked != first["CARGO_TARGET_DIR"] and not target.exists()
    (ws / "rust/Cargo.toml").write_text("[profile.dev]\ndebug = 2\n")
    changed = run("r1")
    assert changed["CARGO_TARGET_DIR"] != locked
    Path(changed["CARGO_TARGET_DIR"]).mkdir(parents=True)
    (Path(changed["CARGO_TARGET_DIR"]) / "big").write_bytes(b"x" * 4096)
    assert run("r1", cap="0")["CARGO_TARGET_DIR"] == changed["CARGO_TARGET_DIR"]
    assert not (Path(changed["CARGO_TARGET_DIR"]) / "big").exists()
    # A non-numeric cap falls back to 40 GB instead of failing the step.
    Path(changed["CARGO_TARGET_DIR"]).mkdir(parents=True, exist_ok=True)
    (Path(changed["CARGO_TARGET_DIR"]) / "keep").write_text("x")
    assert run("r1", cap="abc")["CARGO_TARGET_DIR"] == changed["CARGO_TARGET_DIR"]
    assert (Path(changed["CARGO_TARGET_DIR"]) / "keep").exists()


def test_self_hosted_pull_request_builds_into_ephemeral_dir_outside_persistent_root(tmp_path):
    script = _rust_prep_script()
    ws = tmp_path / "ws"
    (ws / "rust").mkdir(parents=True)
    (ws / "rust/Cargo.toml").write_text("x")
    home = tmp_path / "home"
    home.mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    targets = []
    for _ in range(2):
        out = tmp_path / "env"
        out.write_text("")
        subprocess.run(["bash", "-c", script], cwd=ws, check=True, env={
            "PATH": "/usr/bin:/bin", "HOME": str(home), "RUNNER_TEMP": str(temp),
            "GITHUB_ENV": str(out), "GITHUB_REPOSITORY": "o/r", "CI_CARGO_PERSIST": "0",
            "CI_CARGO_TARGET_CAP_GB": "40"})
        targets.append(dict(l.split("=", 1) for l in out.read_text().splitlines())["CARGO_TARGET_DIR"])
    assert targets[0] != targets[1]
    assert all(Path(t).name == "target" and Path(t).parent.parent == temp and not any(Path(t).parent.iterdir())
               for t in targets)
    assert not (home / "cargo-target").exists()


def test_rust_shards_find_pg_jail_in_the_archive_target_dir_with_and_without_cargo_target_dir():
    jobs = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())["jobs"]
    archive = next(s for s in jobs["rust"]["steps"] if s.get("id") == "archive")["run"]
    target = next(l.strip() for l in archive.splitlines() if l.strip().startswith("target="))
    run = next(s for s in jobs["rust-test"]["steps"] if s.get("name") == "cargo nextest run")["run"]
    jail = run.split("PG_JAIL_BIN=", 1)[1].split(" cargo ", 1)[0]
    base = {"PATH": "/usr/bin:/bin", "GITHUB_WORKSPACE": "/w"}

    def resolve(extra):
        script = f'{target}\nTARGET_DIR="$target"\nprintf %s {jail}'
        return subprocess.run(["bash", "-c", script], env={**base, **extra},
                              capture_output=True, text=True, check=True).stdout

    assert resolve({}) == "/w/rust/target/debug/pg-jail"
    assert resolve({"CARGO_TARGET_DIR": "/t/x/target"}) == "/t/x/target/debug/pg-jail"


def test_rust_tests_run_in_three_slices_of_one_archive():
    jobs = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())["jobs"]
    build, shards = jobs["rust"], jobs["rust-test"]
    upload = next(s for s in build["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    assert upload["with"]["name"] == "rust-tests" and upload["with"]["if-no-files-found"] == "error"
    assert "cargo nextest archive" in next(s for s in build["steps"] if s.get("id") == "archive")["run"]
    assert shards["needs"] == ["select", "rust"] and shards["if"] == "needs.select.outputs.rust == 'true'"
    assert shards["strategy"] == {"fail-fast": False, "matrix": {"shard": [1, 2, 3]}}
    assert shards["name"] == "Rust workspace tests (shard ${{ matrix.shard }} of 3)"
    download = next(s for s in shards["steps"] if str(s.get("uses", "")).startswith("actions/download-artifact"))
    assert download["with"]["name"] == "rust-tests"
    run = next(s for s in shards["steps"] if s.get("name") == "cargo nextest run")["run"]
    for arg in ('--partition "slice:${{ matrix.shard }}/3"', "--archive-file", "--profile ci",
                '--extract-to "$(dirname "$TARGET_DIR")"'):
        assert arg in run
    assert "rust-test" in jobs["ci-gate"]["needs"]
    assert all(SETUP_NEXTEST in [s.get("uses") for s in job["steps"]] for job in (build, shards))


def reasonless_overrides(text):
    """Headers of nextest overrides that change a timeout without a `reason:` line in the comment above them."""
    lines = text.splitlines()
    found = []
    for index, line in enumerate(lines):
        if not re.fullmatch(r"\[\[profile\.[\w-]+\.overrides\]\]", line.strip()):
            continue
        body = []
        for following in lines[index + 1:]:
            if following.startswith("["):
                break
            body.append(following)
        comments = []
        for previous in reversed(lines[:index]):
            if not previous.startswith("#"):
                break
            comments.append(previous)
        if any(re.match(r"\s*(slow-timeout|leak-timeout)\s*=", b) for b in body) and not any(
                "reason:" in c for c in comments):
            found.append(line.strip())
    return found


def test_every_rust_test_has_a_hang_limit_and_each_longer_limit_a_reason():
    text = (ROOT / "rust/.config/nextest.toml").read_text()
    config = tomllib.loads(text)
    limit = config["profile"]["default"]["slow-timeout"]
    assert re.fullmatch(r"\d+s", limit["period"]) and 1 <= limit["terminate-after"] <= 5
    assert {"path": "debug/pg-jail", "relative-to": "target", "on-missing": "error"} in (
        config["profile"]["default"]["archive"]["include"])
    assert reasonless_overrides(text) == []
    exempt = "[[profile.default.overrides]]\nfilter = 'test(=slow)'\nslow-timeout = { period = \"600s\" }\n"
    assert reasonless_overrides(exempt) == ["[[profile.default.overrides]]"]
    assert reasonless_overrides("# reason: builds a crate\n" + exempt) == []
