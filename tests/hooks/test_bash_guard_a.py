#!/usr/bin/env python3
"""FLEET-HOOKS (P0): tests/hooks/bash_guard.py, the PreToolUse guard for the
Bash tool.

Every case runs the hook as a real subprocess with the hook's own PreToolUse
JSON on stdin - exactly the shape and transport Claude Code uses - and reads
back the exit code and stderr text. Nothing here imports the hook module
directly: a hook that behaves correctly when called but not when run is not
proven at all.

CASES is one table: each row is a shell command plus whether the guard must
deny it and, when it denies, a substring its stderr message must carry. The
rows cover the two deny shapes (an unquoted heredoc whose body carries a
backtick/`$(`, and a `claude --bg`/`-p`/`--print` call whose double-quoted
argument carries one) and every false-positive guard named in the brief:
quoted heredoc delimiters (`'EOF'`, `"EOF"`, `\\EOF`), a literal `<<` inside
a double-quoted string, the `$((...))` arithmetic shift operator, a
single-quoted or escaped backtick, a bare `$VAR`, an unrelated flag, and text
after a heredoc's terminator line.

This file holds the heredoc and `claude` halves of that table (it continues
in test_bash_guard_b.py); the runners and the per-row test factory live in
_helpers.py. Run directly:

    python3 tests/hooks/test_bash_guard_a.py
"""
import unittest

from _helpers import _make_case_test

# (name, command, expect_deny, expect_substring_in_stderr_or_None)
CASES = [
    # ─── heredoc: the incident shape and its variants (DENY) ──────────
    ("incident_shape_unquoted_heredoc_backtick_prose",
     "cat <<EOF\nIf this fails, run:\n`netplan apply`\nEOF\n",
     True, "heredoc"),
    ("dash_heredoc_tab_terminator_with_command_substitution",
     "cat <<-EOF\n\tsome text $(whoami)\n\tEOF\n",
     True, "heredoc"),
    ("unquoted_heredoc_dollar_paren_no_backtick",
     "cat <<EOF\nrun $(id)\nEOF\n",
     True, "heredoc"),
    ("delimiter_reuse_first_clean_second_dirty",
     "cat <<EOF\nclean body\nEOF\ncat <<EOF\ndirty `id`\nEOF\n",
     True, "heredoc"),
    ("delimiter_reuse_first_dirty_second_clean",
     "cat <<EOF\ndirty `id`\nEOF\ncat <<EOF\nclean body\nEOF\n",
     True, "heredoc"),
    ("heredoc_inside_command_substitution",
     "x=$(cat <<EOF\nhello `id`\nEOF\n)\n",
     True, "heredoc"),
    ("heredoc_before_pipe_with_dirty_body",
     "cat <<EOF | grep x\nbad `id`\nEOF\n",
     True, "heredoc"),
    ("custom_delimiter_name_dirty",
     "cat <<PROMPT\ntext `id`\nPROMPT\n",
     True, "heredoc"),

    # ─── heredoc false-positive guards (ALLOW) ─────────────────────────
    ("single_quoted_delimiter_allows_backtick_body",
     "cat <<'EOF'\n`id`\nEOF\n",
     False, None),
    ("double_quoted_delimiter_allows_backtick_body",
     'cat <<"EOF"\n`id`\nEOF\n',
     False, None),
    ("backslash_escaped_delimiter_allows_backtick_body",
     "cat <<\\EOF\n`id`\nEOF\n",
     False, None),
    ("literal_angle_brackets_in_double_quoted_string",
     'echo "a << b"',
     False, None),
    ("arithmetic_left_shift_not_heredoc",
     "echo $((1<<3))",
     False, None),
    ("clean_heredoc_body_allows",
     "cat <<EOF\nnothing dangerous here\nEOF\n",
     False, None),
    ("text_after_terminator_is_not_body",
     "cat <<EOF\nfoo\nEOF\necho 'bar `cmd`'\n",
     False, None),
    ("comment_hides_heredoc_operator",
     "echo hi # cat <<EOF",
     False, None),

    # ─── claude --bg / -p / --print (DENY) ─────────────────────────────
    ("claude_bg_backtick_prompt",
     'claude --bg "do `whoami`"',
     True, "claude"),
    ("claude_dash_p_dollar_paren_prompt",
     'claude -p "run $(id)"',
     True, "claude"),
    ("claude_print_long_flag_backtick_prompt",
     'claude --print "text `ls`"',
     True, "claude"),
    ("claude_prompt_before_bg_flag",
     'claude "cmd `id`" --bg',
     True, "claude"),
    ("claude_other_flags_interspersed",
     'claude --model x --bg --verbose "report $(pwd)"',
     True, "claude"),
    ("claude_absolute_path_invocation",
     '/usr/local/bin/claude --bg "r `id`"',
     True, "claude"),
    ("claude_env_assignment_prefix",
     'FOO=bar claude --bg "x `id`"',
     True, "claude"),
    ("claude_after_and_and",
     'echo hi && claude --bg "x `id`"',
     True, "claude"),
    ("claude_after_pipe",
     'echo hi | claude -p "x `id`"',
     True, "claude"),

    # ─── claude false-positive guards (ALLOW) ──────────────────────────
    ("claude_bg_plain_text_allows",
     'claude --bg "plain text"',
     False, None),
    ("claude_bg_single_quoted_backtick_allows",
     "claude --bg 'has a backtick `here`'",
     False, None),
    ("claude_bg_escaped_backtick_allows",
     r'claude --bg "uses \`escaped\` backtick"',
     False, None),
    ("claude_bg_plain_dollar_var_allows",
     'claude --bg "uses $HOME only"',
     False, None),
    ("claude_no_bg_or_print_flag_allows",
     'claude "no bg or print flag `here`"',
     False, None),
    ("claude_unrelated_flag_allows",
     'claude --verbose "no special flag `here`"',
     False, None),
    # over-deny: harmless
    ("claude_dash_prefix_dirty_denied",
     'claude -prefix "not the -p flag `here`"',
     True, "claude"),
    # over-deny: harmless
    ("claude_dash_print_dirty_denied",
     'claude -print "not the -p flag `here`"',
     True, "claude"),
    # over-deny: harmless
    ("claude_dash_pretty_dirty_denied",
     'claude -pretty "not the -p flag `here`"',
     True, "claude"),
    ("non_claude_command_with_bg_flag_allows",
     'somecmd --bg "x `id`"',
     False, None),

    # ─── combined ───────────────────────────────────────────────────
    ("heredoc_and_claude_both_clean_allows",
     'cat <<EOF\nfine\nEOF\nclaude --bg "ok"\n',
     False, None),

    # ─── round 2: wrappers (item 1) ───────────────────────────────────
    ("sudo_claude_bg_denied",
     'sudo claude --bg "do $(whoami)"',
     True, "claude"),
    ("sudo_claude_bg_allows",
     'sudo claude --bg "plain text"',
     False, None),
    ("sudo_u_root_claude_p_denied",
     'sudo -u root claude -p "run $(id)"',
     True, "claude"),
    ("sudo_u_root_claude_p_allows",
     'sudo -u root claude -p "plain text"',
     False, None),
    ("env_var_claude_p_denied",
     'env VAR=1 claude -p "run $(id)"',
     True, "claude"),
    ("env_var_claude_p_allows",
     'env VAR=1 claude -p "plain text"',
     False, None),
    ("env_i_claude_print_denied",
     'env -i claude --print "text $(whoami)"',
     True, "claude"),
    ("env_i_claude_print_allows",
     'env -i claude --print "plain text"',
     False, None),
    ("nohup_claude_bg_denied",
     'nohup claude --bg "run $(id)"',
     True, "claude"),
    ("nohup_claude_bg_allows",
     'nohup claude --bg "plain text"',
     False, None),
    ("time_claude_p_denied",
     'time claude -p "run $(id)"',
     True, "claude"),
    ("time_claude_p_allows",
     'time claude -p "plain text"',
     False, None),
    ("exec_claude_bg_denied",
     'exec claude --bg "run $(id)"',
     True, "claude"),
    ("exec_claude_bg_allows",
     'exec claude --bg "plain text"',
     False, None),
    ("timeout_claude_p_denied",
     'timeout 5 claude -p "run $(id)"',
     True, "claude"),
    ("timeout_claude_p_allows",
     'timeout 5 claude -p "plain text"',
     False, None),
    ("nice_claude_print_denied",
     'nice -n 5 claude --print "text $(id)"',
     True, "claude"),
    ("nice_claude_print_allows",
     'nice -n 5 claude --print "plain text"',
     False, None),
    ("wrapper_non_claude_backtick_allows",
     'sudo ls "`id`"',
     False, None),
    ("env_u_claude_p_denied",
     'env -u HOME claude -p "$(id)"',
     True, "claude"),
    ("env_u_claude_p_allows",
     'env -u HOME claude -p "plain text"',
     False, None),
    ("command_claude_p_denied",
     'command claude -p "$(id)"',
     True, "claude"),
    ("command_claude_p_allows",
     'command claude -p "plain text"',
     False, None),
    ("builtin_claude_p_denied",
     'builtin claude -p "$(id)"',
     True, "claude"),
    ("builtin_claude_p_allows",
     'builtin claude -p "plain text"',
     False, None),
    ("setsid_claude_p_denied",
     'setsid claude -p "$(id)"',
     True, "claude"),
    ("setsid_claude_p_allows",
     'setsid claude -p "plain text"',
     False, None),
    ("sudo_terminator_claude_p_denied",
     'sudo -- claude -p "$(id)"',
     True, "claude"),
    ("sudo_terminator_claude_p_allows",
     'sudo -- claude -p "plain text"',
     False, None),
    ("sudo_u_root_terminator_claude_p_denied",
     'sudo -u root -- claude -p "$(id)"',
     True, "claude"),
    ("sudo_u_root_terminator_claude_p_allows",
     'sudo -u root -- claude -p "plain text"',
     False, None),

    # ─── round 2: flag with = (item 2) ────────────────────────────────
    ("claude_print_equals_denied",
     'claude --print="$(id)"',
     True, "claude"),
    ("claude_print_equals_allows",
     'claude --print="plain"',
     False, None),
]

class BashGuardCaseTests(unittest.TestCase):
    """One test per CASES row, generated below."""


for _name, _command, _deny, _substr in CASES:
    setattr(BashGuardCaseTests, "test_" + _name,
            _make_case_test(_command, _deny, _substr))


if __name__ == "__main__":
    unittest.main(verbosity=2)
