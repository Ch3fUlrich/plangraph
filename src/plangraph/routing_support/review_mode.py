"""Review mode lookup and standalone OpenCode prompt-only isolation."""
import json
import os


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
    and plugins regardless of discovered project configuration as well.
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
    return ["env", "OPENCODE_CONFIG_CONTENT=" + json.dumps(config),
            *cmd, "--agent", "lean-review"]
