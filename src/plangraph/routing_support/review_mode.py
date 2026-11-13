"""Review mode lookup and standalone OpenCode prompt-only isolation."""
import atexit
import json
import os
import subprocess
import sys
import tempfile
from importlib.resources import files
from pathlib import Path


def _contract_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate prompt transport contract key")
        result[key] = value
    return result


def load_prompt_transport_contract(raw=None):
    """Read packaged v1 data; optional JSON must match the authoritative document.

    This loads data only: admission/grammar enforcement belongs to later slices.
    Return a fresh object so consumers cannot mutate another caller's contract.
    """
    text = (files("plangraph").joinpath("config/prompt_transport_v1.json")
            .read_text(encoding="utf-8") if raw is None else raw)
    doc = json.loads(text, object_pairs_hook=_contract_object)
    if not isinstance(doc, dict) or type(doc.get("version")) is not int or doc["version"] != 1:
        raise ValueError("unsupported prompt transport contract version")
    reasons = doc.get("reasons")
    if (not isinstance(reasons, dict) or set(reasons) != set("PCAUFMEXS")
            or not all(isinstance(name, str) and name for name in reasons.values())
            or len(set(reasons.values())) != len(reasons)):
        raise ValueError("invalid or duplicate prompt transport reasons")
    if raw is not None:
        authoritative = load_prompt_transport_contract()
        if json.dumps(doc, sort_keys=True) != json.dumps(authoritative, sort_keys=True):
            raise ValueError("unknown prompt transport contract data or reason name")
    modes, names = doc.get("modes"), set()
    if not isinstance(modes, list) or not modes:
        raise ValueError("missing prompt transport modes")
    for mode in modes:
        if not isinstance(mode, dict) or not isinstance(mode.get("aliases"), list):
            raise ValueError("invalid prompt transport mode")
        for name in [mode.get("harness"), *mode["aliases"]]:
            if not isinstance(name, str) or not name or name in names:
                raise ValueError("invalid or duplicate prompt transport harness")
            names.add(name)
    return doc


def prompt_transport_reason(reason_id):
    """Resolve a reason ID without introducing a second spelling table."""
    reasons = load_prompt_transport_contract()["reasons"]
    if not isinstance(reason_id, str) or reason_id not in reasons:
        raise ValueError("unknown prompt transport reason ID")
    return reasons[reason_id]


def prompt_transport_reason_id(name):
    """Refuse unknown names; diagnostic text is not a reason lookup input."""
    for reason_id, known in load_prompt_transport_contract()["reasons"].items():
        if name == known:
            return reason_id
    raise ValueError("unknown prompt transport reason name")


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
