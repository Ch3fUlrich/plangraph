"""pg-policy is built once, outside the 60 s policy bound, into the shared capped target."""
import importlib.util
import os
from pathlib import Path
import shutil
import stat
import sys
import time

import pytest

from test_preflight import preflight, repo  # noqa: F401

TOOLS = Path(__file__).resolve().parents[1] / "tools"
PY = f"#!{sys.executable}\n"


def script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PY + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def rig(repo, monkeypatch):
    """A checkout with a rust workspace, stub policy tools and a stub cargo that builds slowly."""
    (repo / "rust").mkdir()
    (repo / "rust/Cargo.toml").write_text("[workspace]\nmembers = []\n")
    tools = repo / "ptools"
    for name in ("check_file_size.py", "check_changelog_docs.py", "ci_commit_gate.py"):
        script(tools / name, "")
    script(tools / "split/scrub_check.py", "")
    shutil.copy(TOOLS / "check_new_code_lang.py", tools / "check_new_code_lang.py")
    log = repo / "cargo.log"
    seen = repo / "pg-policy.log"
    script(repo / "bin/cargo", f"""import os, sys, time
from pathlib import Path
open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')
if sys.argv[1] == 'build':
    time.sleep(float(os.environ.get('STUB_BUILD_SECONDS', '0')))
    if os.environ.get('STUB_BUILD_FAILS'):
        print('error: could not compile pg-policy')
        raise SystemExit(101)
    out = Path(os.environ['CARGO_TARGET_DIR']) / 'debug/pg-policy'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text({PY!r} + "open({str(seen)!r}, 'a').write(' '.join(__import__('sys').argv[1:]) + chr(10))\\n")
    out.chmod(0o755)
elif sys.argv[1] == 'run':
    time.sleep(float(os.environ.get('STUB_BUILD_SECONDS', '0')))
""")
    monkeypatch.setenv("PREFLIGHT_TOOLS_DIR", str(tools))
    monkeypatch.setenv("PATH", f"{repo / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(repo / "target"))
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_CAP_GB", "100")
    monkeypatch.delenv("RUSTC_WRAPPER", raising=False)
    return repo, log, seen


def policy_line(out):
    return [line for line in out.splitlines() if line.split(" ")[:1] in (["PASS"], ["FAIL"]) and " policy " in line]


def test_cold_build_counts_outside_the_policy_check_bound(rig, monkeypatch, capsys):
    repo, log, seen = rig
    monkeypatch.setattr(preflight, "CHECK_TIMEOUT", 3)
    monkeypatch.setenv("STUB_BUILD_SECONDS", "5")   # longer than the bound
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 0
    out = capsys.readouterr().out
    assert "PASS policy" in out, out
    assert log.read_text().count("build ") == 1
    assert "run " not in log.read_text()            # the check never compiled itself
    calls = seen.read_text().splitlines()
    assert len(calls) == 2 and calls[0].startswith("--test-sources")


def test_the_built_binary_is_a_private_copy_removed_afterwards(rig, monkeypatch):
    repo, log, seen = rig
    cleanup = []
    built = repo / "target/debug/pg-policy"
    built.parent.mkdir(parents=True)
    built.write_text("x")
    staged = preflight.stage_policy_binary(repo / "target", cleanup)
    assert Path(staged).read_text() == "x" and Path(staged) != built
    assert str(Path(staged).parent) in cleanup
    built.write_text("replaced by another checkout")
    assert Path(staged).read_text() == "x"
    assert preflight.stage_policy_binary(repo / "none", cleanup) is None


def test_without_the_prebuilt_binary_the_check_falls_back_to_cargo_run(rig, monkeypatch):
    spec = importlib.util.spec_from_file_location("cncl", TOOLS / "check_new_code_lang.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []
    monkeypatch.setattr(mod.subprocess, "call", lambda command: calls.append(command) or 0)
    monkeypatch.delenv("PG_POLICY_BIN", raising=False)
    assert mod.main() == 0
    assert calls[0][:2] == ["cargo", "run"] and "--test-sources" in calls[0]
    calls.clear()
    monkeypatch.setenv("PG_POLICY_BIN", __file__)
    assert mod.main() == 0
    assert [c[0] for c in calls] == [__file__, __file__]


def test_a_failed_policy_build_fails_policy_without_compiling_inside_the_check(rig, monkeypatch, capsys):
    repo, log, seen = rig
    monkeypatch.setenv("STUB_BUILD_FAILS", "1")
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 1
    out = capsys.readouterr().out
    assert "FAIL policy" in out and "pg-policy build: error: could not compile pg-policy" in out
    assert "run " not in log.read_text()


def test_no_rust_workspace_means_no_prebuild(repo, monkeypatch):
    assert preflight.policy_build_command(repo) is None


def test_the_prebuild_uses_the_shared_target_and_rust_step_target(rig, monkeypatch, capsys):
    repo, log, seen = rig
    preflight.main(["--base", "HEAD", "--fast"], repo)
    assert (repo / "target/debug/pg-policy").is_file()
