"""D-6517 findings of the UI auto deploy: the shipped templates must not publish the plan page
anonymously, must render only from a verified revision and must keep gate state out of the output dir."""
import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NGINX = ROOT / "deploy" / "nginx" / "plan-page.conf.example"
UNITS = ROOT / "deploy" / "systemd"
SERVICE = UNITS / "plangraph-render.service"
DROPIN = UNITS / "plangraph-render.service.d" / "live-tree.conf.example"
ENV_EXAMPLE = UNITS / "render.env.example"


def directives(text):
    """The non-comment directive lines of a config text, stripped."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def plan_block(text):
    match = re.search(r"location\s*=\s*/plan\s*\{(.*?)\n\}", text, re.S)
    assert match, "the template must own `location = /plan`"
    return match.group(1)


def test_plan_location_denies_anonymous_requests():
    block = directives(plan_block(NGINX.read_text(encoding="utf-8")))
    assert any(re.fullmatch(r'auth_basic\s+"[^"]+";', d) for d in block), "auth_basic realm required"
    assert any(re.fullmatch(r"auth_basic_user_file\s+\S+;", d) for d in block), "a user file is required"
    assert not any(d.startswith("auth_basic off") or d.startswith("satisfy any") for d in block)
    assert not any(d.startswith(("allow all", "auth_request off")) for d in block)


def exec_start():
    lines = [line for line in SERVICE.read_text(encoding="utf-8").splitlines() if line.startswith("ExecStart=")]
    assert len(lines) == 1
    return lines[0][len("ExecStart="):]


def test_render_runs_from_an_exported_copy_of_the_verified_commit():
    start = exec_start()
    gate = start.find("plangraph.live_follow export")
    assert gate != -1, "ExecStart must export the verified commit first"
    assert gate < start.find("mktemp"), "the export must run before anything is rendered"
    assert gate < start.find("cat "), "the export must run before anything is published"
    assert 'PYTHONPATH="$R/src"' in start[gate:], "the render must import from the exported copy"


def test_gate_code_runs_from_a_trusted_install_never_the_live_checkout():
    text = SERVICE.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "plangraph.live_follow" in line and not line.startswith("#"):
            assert 'PYTHONPATH="$PLANGRAPH_TRUSTED_SRC"' in line, line
            assert "PLANGRAPH_LIVE_TREE/src" not in line, line


def run_service_script(tmp_path, sha_out="abcdef0123", export_rc=0):
    """Run the real ExecStart script with a fake python3, under a path containing spaces."""
    root = tmp_path / "a dir with spaces"
    (root / "state").mkdir(parents=True)
    out = root / "out dir"
    out.mkdir()
    bin_dir = root / "bin"
    bin_dir.mkdir()
    log = root / "calls.log"
    fake = bin_dir / "python3"
    fake.write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        f'  *live_follow*export*) echo "live_follow PYTHONPATH=$PYTHONPATH" >> {shlex.quote(str(log))};'
        f' [ {export_rc} -eq 0 ] && printf "%s\\n" {sha_out}; exit {export_rc};;\n'
        f'  *keeper.cli*render*) echo "render PYTHONPATH=$PYTHONPATH SHA=$PLANGRAPH_BUILD_SHA" >> {shlex.quote(str(log))};'
        ' out="${@: -1}"; [ "$out" = ">/dev/null" ] && out="${@: -2:1}"; echo "<html></html>" > "$out"; exit 0;;\n'
        "esac\nexit 9\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PLANGRAPH_TRUSTED_SRC": str(root / "trusted src"),
        "PLANGRAPH_LIVE_TREE": str(root / "live"),
        "PLANGRAPH_STATE": str(root / "state" / "s.json"),
        "PLANGRAPH_OUT": str(out),
    }
    argv = shlex.split(exec_start())
    done = subprocess.run(argv, env=env, capture_output=True, text=True)
    return done, root, out, log


def test_service_handoff_survives_paths_with_spaces(tmp_path):
    done, root, out, log = run_service_script(tmp_path)
    assert done.returncode == 0, done.stderr
    assert (out / "plan-cdn.html").read_text() == "<html></html>\n"
    calls = log.read_text().splitlines()
    assert calls[0] == f"live_follow PYTHONPATH={root / 'trusted src'}"
    assert calls[1] == f"render PYTHONPATH={root / 'state' / 'render-tree' / 'src'} SHA=abcdef0123"


def test_service_keeps_the_old_page_when_export_fails(tmp_path):
    done, _, out, log = run_service_script(tmp_path, export_rc=3)
    assert done.returncode != 0
    assert not (out / "plan-cdn.html").exists()
    assert "render" not in log.read_text()


def test_service_requires_the_state_path(tmp_path):
    assert "PLANGRAPH_STATE:?" in exec_start() or 'PLANGRAPH_STATE"' in exec_start()


def test_live_tree_dropin_sets_a_private_state_path_outside_the_output_dir():
    env = dict(re.findall(r"^Environment=(\w+)=(.*)$", DROPIN.read_text(encoding="utf-8"), re.M))
    state, out = env.get("PLANGRAPH_STATE"), env["PLANGRAPH_OUT"]
    assert state, "the drop-in clears EnvironmentFile, so it must set PLANGRAPH_STATE itself"
    assert not state.startswith(out.rstrip("/") + "/"), "state must not live inside the web-served directory"


def test_env_example_state_path_is_not_under_out():
    env = dict(re.findall(r"^(PLANGRAPH_\w+)=(.*)$", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M))
    assert env["PLANGRAPH_STATE"] and not env["PLANGRAPH_STATE"].startswith(env["PLANGRAPH_OUT"].rstrip("/") + "/")


def test_trusted_src_is_set_in_the_dropin_and_documented():
    dropin = DROPIN.read_text(encoding="utf-8")
    assert re.search(r"^Environment=PLANGRAPH_TRUSTED_SRC=\S+", dropin, re.M)
    assert "PLANGRAPH_TRUSTED_SRC=" in ENV_EXAMPLE.read_text(encoding="utf-8")
