"""D-6517 findings of the UI auto deploy: the shipped templates must not publish the plan page
anonymously, must render only from a verified revision and must keep gate state out of the output dir."""
import re
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


def test_render_runs_from_an_exported_copy_of_the_verified_commit():
    lines = [line for line in SERVICE.read_text(encoding="utf-8").splitlines() if line.startswith("ExecStart=")]
    assert len(lines) == 1
    start = lines[0]
    gate = start.find("plangraph.live_follow export")
    assert gate != -1, "ExecStart must export the verified commit first"
    assert gate < start.find("mktemp"), "the export must run before anything is rendered"
    assert gate < start.find("cat "), "the export must run before anything is published"
    after = start[gate:]
    assert 'PYTHONPATH="$R/src"' in after, "the render must import from the exported copy"
    assert 'PYTHONPATH="$PLANGRAPH_LIVE_TREE/src"; python3 -m plangraph.keeper' not in start


def test_live_tree_dropin_sets_a_private_state_path_outside_the_output_dir():
    env = dict(re.findall(r"^Environment=(\w+)=(.*)$", DROPIN.read_text(encoding="utf-8"), re.M))
    state, out = env.get("PLANGRAPH_STATE"), env["PLANGRAPH_OUT"]
    assert state, "the drop-in clears EnvironmentFile, so it must set PLANGRAPH_STATE itself"
    assert not state.startswith(out.rstrip("/") + "/"), "state must not live inside the web-served directory"


def test_env_example_state_path_is_not_under_out():
    env = dict(re.findall(r"^(PLANGRAPH_\w+)=(.*)$", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M))
    assert env["PLANGRAPH_STATE"] and not env["PLANGRAPH_STATE"].startswith(env["PLANGRAPH_OUT"].rstrip("/") + "/")
