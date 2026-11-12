"""Prompt transport contracts, review mode lookup and prompt-only isolation."""
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


class PromptTransportError(RuntimeError):
    """A malformed prompt carrier is a named pre-exec refusal."""


def launch_stdin_payload(cmd, prompt, exe, *, is_nt=False):
    """Feed known stdin modes only; other helpers retain their argv unchanged."""
    cmd = cmd[3:] if is_prompt_only_command(cmd) else cmd
    harnesses = {'opencode', 'claude', 'agy', 'qodercli', 'codex', 'gemini', 'qwen'}
    if cmd and (cmd[0] not in harnesses or (cmd[0] == 'claude' and '--bg' in cmd)):
        return None  # No new stdin contract for legacy/argv-only commands.
    unknown = 'stdin_prompt_command_unknown: command mode has no stdin contract'
    if not cmd:
        raise PromptTransportError(unknown)
    harness = cmd[0]
    value_flags = {'--model', '--port', '--agent', '--permission-mode', '--setting-sources',
                   '--allowedTools', '--disallowedTools', '--tools', '--mcp-config',
                   '--input-format', '--output-format', '--format', '--title'}
    if harness != 'claude':
        value_flags.add('-p')
    options, positional, inline = {}, [], []
    args = iter(cmd[1:])
    for arg in args:
        if arg == '--':
            positional.extend(args)
            break
        flag, sep, value = arg.partition('=')
        if flag in value_flags:
            options.setdefault(flag, []).append(value if sep else next(args, None))
        elif arg.startswith('-') and arg != '-':
            options.setdefault(flag, []).append(value if sep else True)
            if sep:
                inline.append(value)
        else:
            positional.append(arg)
    if harness in ('qodercli', 'agy') and options.get('-p') != ['']:
        raise PromptTransportError('stdin_prompt_flag_missing: expected -p with an empty value')
    expected = ['run'] if harness == 'opencode' else ['exec', '-'] if harness == 'codex' else []
    mode_ok = {
        'opencode': cmd[1:2] == ['run'],
        'codex': cmd[1:2] == ['exec'] and '-' in positional,
        'claude': options.get('-p') == [True],
        'agy': options.get('--input-format') == ['stream-json'],
        'qodercli': options.get('--input-format') == ['text'],
        'gemini': options.get('-p') == [''],
        'qwen': options.get('-p') == [''],
    }[harness]
    if (not mode_ok or '--bg' in options or (harness not in {'agy', 'qodercli'}
            and options.get('--input-format', ['text']) != ['text'])):
        raise PromptTransportError(unknown)
    metadata = [value for flag, values in options.items() if flag in value_flags
                and flag not in {'--format', '--input-format', '--output-format'}
                for value in values if isinstance(value, str)]
    if (positional != expected or (prompt and any(prompt in value for value in inline))
            or (prompt and any(value == prompt or (len(prompt) >= 16 and prompt in value) for value in metadata))):
        raise PromptTransportError('stdin_prompt_argv_conflict: prompt already present in argv')
    if harness == 'agy':
        return (json.dumps({'event': 'user', 'message': {'role': 'user', 'content': prompt}},
                           ensure_ascii=False) + '\n').encode('utf-8')
    return prompt.encode('utf-8')


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
