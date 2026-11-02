"""Bash guard (tools/hooks/bash_guard*.py): commands it must DENY.

One probe per review finding and per invariant of the fix round:

- G1 every simple command is checked wherever it sits: subshells, groups,
  compound keywords, ``$( )``, backticks, ``-c`` strings, behind wrappers;
- G2 quote state persists across newlines, ``$'..'`` escapes are handled,
  ``$(``, backticks and ``<<`` inside double quotes are seen;
- G3 ``-c`` inside an option cluster takes the NEXT word, ``-o``/``-O`` take
  an argument;
- G4 fail closed above 1 MiB, past depth 3 and on parse errors.

Each probe is a real shell hazard: the calling shell would run the
substitution. Rows are (test id, command, substring of the deny message).
"""
import shlex

from bash_guard_support import MIB, cases, guard  # noqa: F401 (guard is a fixture)

S2 = 'claude -p "$(id)"'            # shape 2: a one-shot claude whose prompt is expanded
HD = "cat <<EOF\n`id`\nEOF\n"       # shape 1: an unquoted heredoc whose body runs `id`


def nest(levels, inner="echo ok"):
    """`inner` wrapped in `levels` layers of bash -c '...'."""
    for _ in range(levels):
        inner = "bash -c " + shlex.quote(inner)
    return inner


# G2: the quote state a line opens is still open on the next line.
QUOTE_STATE = [
    ("b1_multiline_double_quote", 'echo "first\nsecond"; ' + HD, "heredoc"),
    ("b1_double_quote_then_and", 'echo "a\nb" && ' + HD, "heredoc"),
    ("b3_multiline_single_quote", "echo 'a\nb'; " + HD, "heredoc"),
    ("b4_ansi_c_escaped_quote", "echo $'a\\'b'; " + HD, "heredoc"),
    ("comment_with_apostrophe", "# it's fine\n" + HD, "heredoc"),
    ("backslash_newline_in_double_quote", 'echo "a\\\nb"; ' + HD, "heredoc"),
    ("multiline_text_in_substitution", 'echo "$(echo a\necho b)"; ' + HD, "heredoc"),
    ("b2_commit_message_heredoc", 'git commit -m "$(cat <<EOF\nmsg `id`\nEOF\n)"', "heredoc"),
    ("b2_dollar_paren_body", 'git commit -m "$(cat <<EOF\nmsg $(id)\nEOF\n)"', "heredoc"),
    ("b2_dash_heredoc", 'x="$(cat <<-EOF\n\tmsg `id`\n\tEOF\n)"', "heredoc"),
    ("heredoc_in_backticks", "echo `cat <<EOF\n\\`id\\`\nEOF\n`", "heredoc"),
    ("after_arithmetic_expansion", "echo $((1<<3)); " + HD, "heredoc"),
    ("after_arithmetic_command", "((1<<3)); " + HD, "heredoc"),
    ("tab_stripped_delimiter", "cat <<-EOF\n\t`id`\n\tEOF\n", "heredoc"),
    ("second_heredoc_dirty", "cat <<A <<B\nclean\nA\n`id`\nB\n", "heredoc"),
    ("delimiter_reused", "cat <<EOF\nok\nEOF\ncat <<EOF\n`id`\nEOF\n", "heredoc"),
    ("heredoc_before_pipe", "cat <<EOF | cat\n`id`\nEOF\n", "heredoc"),
    ("fd_prefixed_heredoc", "cat 3<<EOF\n`id`\nEOF\n", "heredoc"),
    ("escaped_backslash_then_backtick", "cat <<EOF\n\\\\`id`\nEOF\n", "heredoc"),
    ("heredoc_in_subshell", "(" + HD + ")", "heredoc"),
    ("heredoc_in_function", "f() {\n" + HD + "}\n", "heredoc"),
    ("heredoc_in_c_string", "bash -c 'cat <<EOF\n`id`\nEOF'", "heredoc"),
]

# G1: the dangerous command sits somewhere other than the first word.
POSITIONS = [
    ("b5_subshell", "(" + S2 + ")", "claude"),
    ("b5_subshell_spaced", "( " + S2 + " )", "claude"),
    ("b5_nested_subshell", "( ( " + S2 + " ) )", "claude"),
    ("b5_subshell_newlines", "(\n" + S2 + "\n)", "claude"),
    ("b6_command_substitution", "echo $(" + S2 + ")", "claude"),
    ("b6_backticks", "echo `" + S2 + "`", "claude"),
    ("assignment_substitution", "x=$(" + S2 + ")", "claude"),
    ("double_quoted_substitution", 'echo "$(' + S2 + ')"', "claude"),
    ("process_substitution", "cat <(" + S2 + ")", "claude"),
    ("b7_brace_group", "{ " + S2 + "; }", "claude"),
    ("b7_brace_group_newlines", "{\n" + S2 + "\n}", "claude"),
    ("b8_if", "if " + S2 + "; then :; fi", "claude"),
    ("b8_if_negated", "if ! " + S2 + "; then :; fi", "claude"),
    ("b8_then", "if true; then " + S2 + "; fi", "claude"),
    ("b8_elif", "if false; then :; elif " + S2 + "; then :; fi", "claude"),
    ("b8_else", "if false; then :; else " + S2 + "; fi", "claude"),
    ("b8_while", "while " + S2 + "; do break; done", "claude"),
    ("b8_until", "until " + S2 + "; do :; done", "claude"),
    ("b8_for_do", "for i in 1 2; do " + S2 + "; done", "claude"),
    ("b8_arith_for_do", "for ((i=0; i<2; i++)); do " + S2 + "; done", "claude"),
    ("b8_case_clause", "case x in x) " + S2 + " ;; esac", "claude"),
    ("b8_case_leading_paren", "case x in (x) " + S2 + " ;; esac", "claude"),
    ("b8_case_second_clause", "case y in x) :;; y) " + S2 + " ;; esac", "claude"),
    ("b8_case_in_substitution", "echo $(case x in x) " + S2 + ";; esac)", "claude"),
    ("b8_negation", "! " + S2, "claude"),
    ("function_body", "f() { " + S2 + "; }; f", "claude"),
    ("function_keyword_body", "function f { " + S2 + "; }; f", "claude"),
    ("env_assignment_prefix", "FOO=1 " + S2, "claude"),
    ("redirect_before_command", ">/dev/null " + S2, "claude"),
    ("fd_redirect_before_command", "2>/dev/null " + S2, "claude"),
    ("line_continuation", 'claude \\\n -p "$(id)"', "claude"),
    ("quoted_command_name", "'claude' -p \"$(id)\"", "claude"),
    ("escaped_command_name", 'cl\\aude -p "$(id)"', "claude"),
    ("ansi_c_command_name", "$'claude' -p \"$(id)\"", "claude"),
    ("command_name_from_substitution", '$(command -v claude) -p "$(id)"', "claude"),
]

# G1: the wrappers the command word is found behind.
WRAPPERS = [
    ("env", "env FOO=1 " + S2, "claude"),
    ("env_i_u", "env -i -u HOME " + S2, "claude"),
    ("time", "time " + S2, "claude"),
    ("time_p", "time -p " + S2, "claude"),
    ("nice_n", "nice -n 10 " + S2, "claude"),
    ("nice_dash_number", "nice -5 " + S2, "claude"),
    ("nohup", "nohup " + S2, "claude"),
    ("sudo_u", "sudo -u root -- " + S2, "claude"),
    ("sudo_E", "sudo -E " + S2, "claude"),
    ("timeout", "timeout 5 " + S2, "claude"),
    ("timeout_options", "timeout -k 1 --signal=TERM 5 " + S2, "claude"),
    ("stdbuf", "stdbuf -oL " + S2, "claude"),
    ("stdbuf_separate_values", "stdbuf -o L -e0 " + S2, "claude"),
    ("setsid", "setsid -f " + S2, "claude"),
    ("command", "command " + S2, "claude"),
    ("command_p", "command -p " + S2, "claude"),
    ("exec", "exec " + S2, "claude"),
    ("exec_a", "exec -a x " + S2, "claude"),
    ("ionice", "ionice -c3 " + S2, "claude"),
    ("b9_systemd_run", "systemd-run --user --scope -p MemoryMax=1G " + S2, "claude"),
    ("systemd_run_terminator", "systemd-run --user --scope -q -p MemoryMax=1G -- " + S2, "claude"),
    ("systemd_run_unit_value", "systemd-run --unit job --collect --wait " + S2, "claude"),
    ("nice_then_systemd_run", "nice -n 10 systemd-run --user -q -p MemoryMax=1G " + S2, "claude"),
    ("xargs", "echo a | xargs " + S2, "claude"),
    ("xargs_n1", "echo a | xargs -n1 " + S2, "claude"),
    ("xargs_replace", "echo a | xargs -I{} " + S2, "claude"),
    ("xargs_file", "xargs -0 -a /tmp/f " + S2, "claude"),
    ("wrapper_chain", "sudo env FOO=1 nice -n 5 timeout 9 nohup " + S2, "claude"),
    ("wrapper_in_group", "{ nice -n 5 " + S2 + "; }", "claude"),
    ("user_named_claude_does_not_hide_it", "sudo -u claude " + S2, "claude"),
]

# G1 + G3: shell -c strings, option clusters and the words that take values.
SHELL_C = [
    ("bash_c", "bash -c '" + S2 + "'", "claude"),
    ("sh_c", "sh -c '" + S2 + "'", "claude"),
    ("zsh_c", "zsh -c '" + S2 + "'", "claude"),
    ("dash_c", "dash -c '" + S2 + "'", "claude"),
    ("ksh_c", "ksh -c '" + S2 + "'", "claude"),
    ("absolute_path", "/bin/bash -c '" + S2 + "'", "claude"),
    ("b10_cluster_ce", "bash -ce '" + S2 + "'", "claude"),
    ("b11_cluster_cv", "bash -cv '" + S2 + "'", "claude"),
    ("cluster_ec", "bash -ec '" + S2 + "'", "claude"),
    ("cluster_lc", "bash -lc '" + S2 + "'", "claude"),
    ("cluster_xec", "bash -xec '" + S2 + "'", "claude"),
    ("c_then_option", "bash -c -e '" + S2 + "'", "claude"),
    ("c_then_two_options", "bash -c -e -x '" + S2 + "'", "claude"),
    ("b12_capital_o_takes_argument", "bash -O extglob -c '" + S2 + "'", "claude"),
    ("lower_o_takes_argument", "bash -o pipefail -c '" + S2 + "'", "claude"),
    ("plus_o_takes_argument", "bash +o history -c '" + S2 + "'", "claude"),
    ("o_then_c_in_one_cluster", "bash -oc pipefail '" + S2 + "'", "claude"),
    ("c_then_o_in_one_cluster", "bash -co pipefail '" + S2 + "'", "claude"),
    ("long_options", "bash --norc --noprofile -c '" + S2 + "'", "claude"),
    ("long_option_with_argument", "bash --rcfile /tmp/rc -c '" + S2 + "'", "claude"),
    ("end_of_options", "sh -c -- '" + S2 + "'", "claude"),
    ("b13_systemd_run_bash_c", "systemd-run --user bash -c '" + S2 + "'", "claude"),
    ("b14_xargs_sh_c", "xargs sh -c '" + S2 + "'", "claude"),
    ("xargs_options_sh_c", "xargs -n1 -I{} sh -c '" + S2 + "' _", "claude"),
    ("env_bash_c", "env -i bash -c '" + S2 + "'", "claude"),
    ("sudo_bash_c", "sudo -u root bash -c '" + S2 + "'", "claude"),
    ("shell_after_a_user_named_like_a_shell", "sudo -u sh bash -c '" + S2 + "'", "claude"),
    ("double_quoted_c_string", 'bash -c "claude -p \\"\\$(id)\\""', "claude"),
    ("backtick_in_c_string", "bash -c 'claude -p \"`id`\"'", "claude"),
    ("ansi_c_c_string", "bash -c $'claude -p \"$(id)\"'", "claude"),
    ("ansi_c_hex_escape", "bash -c $'claude -p \"\\x24(id)\"'", "claude"),
    ("ansi_c_newline_escape", "bash -c $'echo hi\\n" + S2 + "'", "claude"),
    ("three_levels", nest(3, S2), "claude"),
    ("eval", "eval '" + S2 + "'", "claude"),
    ("eval_double_quoted", 'eval "claude -p \\"\\$(id)\\""', "claude"),
    ("eval_joined_words", "eval claude -p '\"$(id)\"'", "claude"),
    ("command_eval", "command eval '" + S2 + "'", "claude"),
]

# Shape 2 spellings: the flag forms and the unquoted trigger.
CLAUDE_FORMS = [
    ("unquoted_substitution", "claude -p $(id)", "unquoted"),
    ("unquoted_backtick", "claude -p `id`", "unquoted"),
    ("unquoted_bg", "claude --bg $(id)", "unquoted"),
    ("unquoted_prefix_inside_word", "claude -p prompt$(id)", "unquoted"),
    ("unquoted_default_expansion", "claude -p ${x:-$(id)}", "unquoted"),
    ("mixed_quotes_word", "claude -p 'a'\"$(id)\"", "double-quoted"),
    ("long_print", 'claude --print "$(id)"', "double-quoted"),
    ("flag_after_prompt", 'claude "$(id)" -p', "double-quoted"),
    ("cluster_pq", 'claude -pq "$(id)"', "double-quoted"),
    ("flag_equals", 'claude --print="$(id)"', "double-quoted"),
    ("bare_flag_equals", "claude --bg=$(id)", "unquoted"),
    ("here_string_prompt", 'claude -p <<< "$(id)"', "double-quoted"),
    ("backtick_in_double_quotes", 'claude -p "run `id` now"', "double-quoted"),
    ("heredoc_prompt", "claude -p <<EOF\nrun `id`\nEOF\n", "heredoc"),
]

# G4: fail closed.
CLOSED = [
    ("depth_four", nest(4), "levels"),
    ("depth_four_with_eval", "eval " + shlex.quote(nest(3, "eval 'echo ok'")), "levels"),
    ("depth_four_dirty", nest(4, S2), "levels"),
    ("oversized", "echo " + "a" * MIB, "1 MiB"),
    ("unterminated_double_quote", 'echo "oops', "could not parse"),
    ("unterminated_single_quote", "echo 'oops", "could not parse"),
    ("unterminated_backtick", "echo `oops", "could not parse"),
    ("unterminated_substitution", "echo $(oops", "could not parse"),
    ("unterminated_subshell", "(echo oops", "could not parse"),
    ("unterminated_ansi_c", "echo $'oops", "could not parse"),
    ("unterminated_parameter_expansion", "echo ${oops", "could not parse"),
    ("unterminated_arithmetic", "echo $((1+2", "could not parse"),
    ("nesting_too_deep", "$(" * 40 + ":" + ")" * 40, "could not parse"),
    ("parse_error_inside_c_string", "bash -c 'echo \"oops'", "could not parse"),
    ("parse_error_after_a_clean_command", "echo ok; echo 'oops", "could not parse"),
]


@cases(QUOTE_STATE + POSITIONS + WRAPPERS + SHELL_C + CLAUDE_FORMS + CLOSED)
def test_denied(guard, name, cmd, needle):
    reason = guard.check(cmd)
    assert reason is not None, "allowed: %r" % cmd
    assert reason.startswith("bash-guard: DENIED"), reason
    assert needle in reason, (needle, reason)


def test_tables_have_unique_ids():
    rows = QUOTE_STATE + POSITIONS + WRAPPERS + SHELL_C + CLAUDE_FORMS + CLOSED
    names = [row[0] for row in rows]
    assert len(names) == len(set(names))
