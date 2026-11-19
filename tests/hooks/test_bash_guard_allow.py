"""Bash guard (tools/hooks/bash_guard*.py): legitimate fleet commands it must ALLOW (G5).

Run in process (``check`` returns None) and through the real hook subprocess
(exit 0, empty stderr), so a false block shows in both.
"""
import os
import subprocess
import sys

from bash_guard_support import HOOK, cases, guard, payload, run_bash  # noqa: F401 (guard is a fixture)

PR_BODY = (
    "gh pr create --title t --body \"$(cat <<'EOF'\n"
    "Summary with `code` and $(subst) text\nEOF\n)\""
)
SINGLE_QUOTED_BODY = (
    "gh pr create --title t --body 'Example:\n\n"
    "cat <<EOF\n`id` and $(id)\nEOF\n\nend'"
)
PYTEST = ("nice -n 10 systemd-run --user --scope -q --slice=agent-tests.slice "
          "-p MemoryMax=1G python3 -m pytest -q tests/hooks")

ALLOWED = [
    ("git_C_push", "git -C /tmp/repo push origin x", None),
    ("pr_create_quoted_heredoc", PR_BODY, None),
    ("commit_quoted_heredoc", "git commit -m \"$(cat <<'EOF'\nmsg `x`\nEOF\n)\"", None),
    ("pytest", "python3 -m pytest -q", None),
    ("pytest_in_slice", PYTEST, None),
    ("claude_p_escaped_backticks", 'claude -p "run \\`id\\` and \\$(id)"', None),
    ("claude_p_single_quoted", "claude -p 'has `id` and $(id)'", None),
    ("claude_p_plain", 'claude -p "plain text"', None),
    ("claude_p_short", 'claude -p "x"', None),
    ("claude_print_short", 'claude --print "x"', None),
    ("claude_bg_resume_uuid", "claude --bg --resume 12345678-1234-1234-1234-123456789abc", None),
    ("claude_resume_uuid", "claude --resume 12345678-1234-1234-1234-123456789abc", None),
    ("claude_resume_bare", "claude --resume", None),
    ("claude_agents", "claude agents", None),
    ("claude_agents_json", "claude agents --json", None),
    ("claude_stop_id", "claude stop 77b56beb", None),
    ("claude_bg_with_env_marker", 'env PLANGRAPH_FLEET_LAUNCH=1 claude --bg -n x-0001 "prompt"', None),
    ("claude_bg_with_leading_marker", 'PLANGRAPH_FLEET_LAUNCH=1 claude --bg --remote-control -n x-0001 "prompt"', None),
    ("claude_interactive_substitution", 'claude "$(id)"', None),
    ("single_quoted_multiline_body", SINGLE_QUOTED_BODY, None),
    ("single_quoted_multiline_echo", "echo 'a\n`b`\nc'; ls", None),
    ("double_quoted_multiline_plain", 'echo "a\nb"; ls', None),
    ("quoted_heredoc_double", 'cat <<"EOF"\n`id`\nEOF\n', None),
    ("quoted_heredoc_backslash", "cat <<\\EOF\n`id`\nEOF\n", None),
    ("arithmetic_shift", "echo $((1<<3))", None),
    ("literal_shift_in_quotes", 'echo "a << b"', None),
    ("bash_c_plain", "bash -c 'echo hi'", None),
    ("bash_ce_plain", "bash -ce 'echo hi'", None),
    ("subshell_plain", "(cd /tmp && ls)", None),
    ("if_plain", "if true; then echo a; fi", None),
    ("xargs_plain", "ls | xargs -n1 echo", None),
    ("systemd_run_plain", "systemd-run --user --scope -q python3 -V", None),
]


@cases(ALLOWED)
def test_allowed(guard, name, cmd, needle):
    assert guard.check(cmd) is None, name
    r = run_bash(cmd)
    assert r.returncode == 0, (name, r.stderr)
    assert r.stderr == b""


def test_allowed_with_fleet_launch_env_marker(guard, monkeypatch):
    cmd = 'claude --bg -n x-0001 "prompt"'
    monkeypatch.setenv("PLANGRAPH_FLEET_LAUNCH", "1")
    assert guard.check(cmd) is None
    env = {**os.environ, "PLANGRAPH_FLEET_LAUNCH": "1"}
    r = subprocess.run([sys.executable, "-I", str(HOOK)], input=payload(cmd).encode("utf-8"),
                       capture_output=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stderr == b""


def test_fleet_launch_marker_still_denies_live_substitution(guard, monkeypatch):
    monkeypatch.setenv("PLANGRAPH_FLEET_LAUNCH", "1")
    reason = guard.check('claude --bg "$(id)"')
    assert reason is not None
    assert "double-quoted" in reason
