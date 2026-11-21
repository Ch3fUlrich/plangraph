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


def shared_target(repo):
    """The shared cargo target, outside the checkout like a real one."""
    return repo.parent / (repo.name + "-cargo")


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
    import json
    print(json.dumps(dict(reason='compiler-message', message=dict(rendered='warning: noise'))))
    print(json.dumps(dict(reason='compiler-artifact', target=dict(name='other', kind=['bin']), executable='/nope')))
    print(json.dumps(dict(reason='compiler-artifact', target=dict(name='pg-policy', kind=['bin']), executable=str(out))))
    source = Path(sys.argv[sys.argv.index('--manifest-path') + 1]).parent / 'pg-policy/src/main.rs'
    marker = source.read_text().replace(chr(10), ' ') if source.exists() else ''
    out.write_text({PY!r} + "open({str(seen)!r}, 'a').write(' '.join(__import__('sys').argv[1:]) + chr(10))\\n# " + marker + chr(10))
    out.chmod(0o755)
elif sys.argv[1] == 'run':
    time.sleep(float(os.environ.get('STUB_BUILD_SECONDS', '0')))
""")
    monkeypatch.setenv("PREFLIGHT_TOOLS_DIR", str(tools))
    monkeypatch.setenv("PATH", f"{repo / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PLANGRAPH_CARGO_TARGET_DIR", str(shared_target(repo)))
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


def test_the_artifact_comes_from_cargo_json_and_is_copied_privately(rig, monkeypatch):
    repo, log, seen = rig
    built = shared_target(repo) / "debug/pg-policy"
    built.parent.mkdir(parents=True)
    built.write_text("x")
    cleanup = []
    staged = preflight.stage_policy_binary(str(built), cleanup)
    assert Path(staged).read_text() == "x" and Path(staged) != built
    assert str(Path(staged).parent) in cleanup
    built.write_text("replaced by another checkout")
    assert Path(staged).read_text() == "x"
    out = "\n".join([
        '{"reason":"compiler-message","message":{"rendered":"warning: noise\\n"}}',
        '{"reason":"compiler-artifact","target":{"name":"dep","kind":["lib"]},"executable":null}',
        '{"reason":"compiler-artifact","target":{"name":"pg-policy","kind":["bin"]},"executable":"/x/pg-policy"}',
        "plain line"])
    executable, text = preflight.policy_artifact(out)
    assert executable == "/x/pg-policy" and "warning: noise" in text and "plain line" in text
    assert preflight.policy_artifact("")[0] is None


def test_the_build_and_the_copy_happen_under_one_shared_lease(rig, monkeypatch):
    """Another checkout's rebuild or a cleanup cannot swap the artifact between build and copy."""
    from plangraph.spawner import host_ready
    repo, log, seen = rig
    held = []
    real_stage = preflight.stage_policy_binary

    def stage(executable, cleanup):
        try:
            with host_ready.cargo_lease(exclusive=True):
                held.append(False)
        except OSError:
            held.append(True)
        return real_stage(executable, cleanup)

    monkeypatch.setattr(preflight, "stage_policy_binary", stage)
    cleanup = []
    code, text, binary = preflight.build_policy(repo, dict(os.environ, CARGO_TARGET_DIR=str(shared_target(repo))), cleanup)
    assert (code, held) == (0, [True]) and Path(binary).is_file()


def test_a_build_that_reports_no_executable_fails_explicitly(rig, monkeypatch, capsys):
    repo, log, seen = rig
    monkeypatch.setattr(preflight, "run", lambda command, cwd, env: (0, ""))
    assert preflight.main(["--base", "HEAD", "--fast"], repo) == 1
    out = capsys.readouterr().out
    assert "pg-policy build reported no usable executable" in out and "FAIL policy" in out


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
    (built,) = (shared_target(repo) / "policy-bin").glob("*/debug/pg-policy")
    assert built.is_file()


def checkout_with_policy(tmp_path, name, source):
    root = tmp_path / name
    (root / "rust/pg-policy/src").mkdir(parents=True)
    (root / "rust/Cargo.toml").write_text("[workspace]\nmembers = []\n")
    (root / "rust/pg-policy/src/main.rs").write_text(source)
    return root


def test_the_policy_target_is_keyed_by_the_sources_not_the_checkout_path(rig, tmp_path):
    a = checkout_with_policy(tmp_path, "a", "fn main() {}")
    b = checkout_with_policy(tmp_path, "b", "fn main() {}")
    c = checkout_with_policy(tmp_path, "c", "fn main() { changed(); }")
    assert preflight.policy_target(a) == preflight.policy_target(b) != preflight.policy_target(c)
    assert preflight.policy_target(a).parent == shared_target(rig[0]) / "policy-bin"
    (a / "rust/Cargo.lock").write_text("lock")
    assert preflight.policy_target(a) != preflight.policy_target(b)


def test_an_interleaved_build_of_another_checkout_cannot_replace_the_staged_artifact(rig, tmp_path, monkeypatch):
    """B builds different sources between A's build and A's copy: A still stages A's binary."""
    repo, log, seen = rig
    a = checkout_with_policy(tmp_path, "a", "A-SOURCES")
    b = checkout_with_policy(tmp_path, "b", "B-SOURCES")
    env = dict(os.environ, CARGO_TARGET_DIR=str(shared_target(repo)))
    real_stage, cleanup_b = preflight.stage_policy_binary, []

    def interleave(executable, cleanup):
        monkeypatch.setattr(preflight, "stage_policy_binary", real_stage)   # once
        assert preflight.build_policy(b, env, cleanup_b)[0] == 0
        return real_stage(executable, cleanup)

    monkeypatch.setattr(preflight, "stage_policy_binary", interleave)
    code, text, staged_a = preflight.build_policy(a, env, [])
    assert code == 0
    assert "A-SOURCES" in Path(staged_a).read_text() and "B-SOURCES" not in Path(staged_a).read_text()


def test_sources_are_hashed_even_when_the_checkout_lives_under_a_target_directory(rig, tmp_path):
    a = checkout_with_policy(tmp_path / "target", "a", "A-SOURCES")
    b = checkout_with_policy(tmp_path / "target", "b", "B-SOURCES")
    assert preflight.policy_target(a) != preflight.policy_target(b)
    same = checkout_with_policy(tmp_path / "target", "c", "A-SOURCES")
    assert preflight.policy_target(a) == preflight.policy_target(same)


def test_build_output_inside_the_crate_does_not_change_the_key(rig, tmp_path):
    a = checkout_with_policy(tmp_path, "a", "A-SOURCES")
    before = preflight.policy_target(a)
    (a / "rust/pg-policy/target/debug").mkdir(parents=True)
    (a / "rust/pg-policy/target/debug/artifact").write_text("built")
    assert preflight.policy_target(a) == before


def test_interleaved_builds_stay_apart_under_a_target_ancestor(rig, tmp_path, monkeypatch):
    repo, log, seen = rig
    a = checkout_with_policy(tmp_path / "target", "a", "A-SOURCES")
    b = checkout_with_policy(tmp_path / "target", "b", "B-SOURCES")
    env = dict(os.environ, CARGO_TARGET_DIR=str(shared_target(repo)))
    real_stage = preflight.stage_policy_binary

    def interleave(executable, cleanup):
        monkeypatch.setattr(preflight, "stage_policy_binary", real_stage)   # once
        assert preflight.build_policy(b, env, [])[0] == 0
        return real_stage(executable, cleanup)

    monkeypatch.setattr(preflight, "stage_policy_binary", interleave)
    code, text, staged_a = preflight.build_policy(a, env, [])
    assert code == 0
    assert "A-SOURCES" in Path(staged_a).read_text() and "B-SOURCES" not in Path(staged_a).read_text()
