"""Review mode lookup and standalone OpenCode prompt-only isolation."""
import atexit
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def inline_config():
    """Only overlay a usable explicit config; otherwise retain normal discovery."""
    try:
        config = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT", "null"))
    except (ValueError, TypeError):
        return None
    if not isinstance(config, dict):
        return None
    if not isinstance(config.get("agents", {}), dict) or not isinstance(config.get("policies", []), list):
        return None
    return config


def review_prompt(prompt, mode, client):
    """Render structured judge evidence with the actual seat capability."""
    if client in (None, "opencode") and mode == "prompt-only" and inline_config() is None:
        mode = "tools"
    render = getattr(prompt, "with_tool_mode", None)
    return render(mode) if render else prompt


def leg_tool_mode(client, model):
    from plangraph.planner.legs import DEFAULT_LEGS
    # Match model first: unknown gateway aliases never need native detection.
    return next((leg.tool_mode for leg in DEFAULT_LEGS
                 if leg.model == model and (client is None or leg.harness == client)), "tools")


def prompt_only_command(cmd):
    """One model step: OpenCode removes tools on the final step (V2 agents).

    `run` accepts a message, not interactive slash commands. Deny MCP, skills
    and plugins regardless of discovered project configuration as well. Carry
    the overlay in a private file, never argv; the launcher retains prompt stdin.
    """
    config = inline_config()
    if config is None:
        return cmd
    config.setdefault("agents", {})["lean-review"] = {
        "mode": "primary", "steps": 1,
        "system": "Judge only the supplied evidence. No tools are available.",
        "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
    }
    config.setdefault("policies", []).extend(
        {"action": action, "resource": "*", "effect": "deny"}
        for action in ("tool.use", "mcp.use", "skill.use", "plugin.use"))
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json",
                                     prefix="lean-review-", delete=False) as stream:
        path = Path(stream.name)
        atexit.register(path.unlink, missing_ok=True)  # Also clean unlaunched/dry-run commands.
        json.dump(config, stream)
    return [sys.executable, str(Path(__file__).resolve()), str(path),
            *cmd, "--agent", "lean-review"]


def is_prompt_only_command(cmd):
    """Recognize our launcher so callers deliver review evidence through stdin."""
    return len(cmd) > 3 and cmd[:2] == [sys.executable, str(Path(__file__).resolve())]


def main(argv=None):
    """Run with file-backed config, inheriting stdio and removing the file on exit."""
    path, *cmd = sys.argv[1:] if argv is None else argv
    env = dict(os.environ)
    env.pop("OPENCODE_CONFIG_CONTENT", None)
    env["OPENCODE_CONFIG"] = path
    try:
        return subprocess.run(cmd, env=env).returncode
    finally:
        Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
