"""Bash guard, fix round r2: expansions nested in double quotes, a stray ``)``, a bounded stdin.

Each probe fails the guard if its fix is reverted. Rows are (test id, command).
"""
import json

from bash_guard_support import cases, guard, payload, run_hook  # noqa: F401 (guard is a fixture)

# Substitutions nested inside ${...}, $((...)) and process substitution, in double quotes and bare.
NESTED = [
    ("dq_brace_default", 'claude -p "${x:-$(id)}"'),
    ("dq_brace_backtick", 'claude -p "${x:-`id`}"'),
    ("dq_brace_nested_brace", 'claude -p "${x:-${y:-$(id)}}"'),
    ("dq_brace_inner_dquote", 'claude -p "${x:-"$(id)"}"'),
    ("dq_brace_after_text", 'claude -p "a ${x:-b} $(id)"'),
    ("dq_arith_with_subst", 'claude -p "$((1+$(id)))"'),
    ("bare_brace_default", "claude -p ${x:-$(id)}"),
    ("bare_arith_with_subst", "claude -p $((1+$(id)))"),
    ("procsub_in_command", 'echo <(claude -p "$(id)")'),
    ("outprocsub_in_command", 'echo >(claude -p "$(id)")'),
    ("brace_in_c_string", "bash -c 'claude -p \"${x:-$(id)}\"'"),
]

# A closing parenthesis nobody opened.
STRAY_PAREN = [
    ("top_level_paren_then_probe", 'echo ok ); claude -p "$(id)"'),
    ("lone_paren", "echo ok )"),
    ("paren_after_group", "(echo a) ); echo b"),
    ("paren_in_c_string", "bash -c 'echo ok ); true'"),
]

# Text that stays allowed: no live substitution, or a literal `)` / `<(`.
STAYS_ALLOWED = [
    ("dq_brace_plain", 'claude -p "${x:-ok}"'),
    ("dq_brace_var", 'claude -p "${HOME} and ${x#a}"'),
    ("dq_procsub_text_is_literal", 'claude -p "${x:-<(id)}"'),
    ("quoted_paren", 'echo ")" \')\''),
    ("case_clause_parens", "case a in (a) echo;; b) echo;; esac"),
    ("subshell_balanced", "(echo a); (echo b)"),
]


@cases(NESTED, "name,cmd")
def test_nested_substitutions_are_denied(guard, name, cmd):
    assert guard.check(cmd), cmd


@cases(STRAY_PAREN, "name,cmd")
def test_unexpected_close_paren_is_denied(guard, name, cmd):
    assert guard.check(cmd), cmd
    assert run_hook(payload(cmd)).returncode == 2


@cases(STAYS_ALLOWED, "name,cmd")
def test_clean_forms_stay_allowed(guard, name, cmd):
    assert guard.check(cmd) in (None, ""), cmd


def test_stdin_over_cap_is_denied_without_reading_it_all(guard, monkeypatch):
    cap = guard.hook.STDIN_MAX
    asked = []

    class Stream:
        def read(self, n=-1):
            asked.append(n)
            return b"{" * (n if n >= 0 else 10 * cap)

    class Stdin:
        buffer = Stream()

    monkeypatch.setattr(guard.hook.sys, "stdin", Stdin())
    assert guard.hook.main() == 2
    assert asked == [cap + 1]


def test_stdin_at_cap_still_parses(guard, monkeypatch):
    import io

    body = json.dumps({"tool_name": "Read", "tool_input": {}}).encode()
    monkeypatch.setattr(guard.hook.sys, "stdin", type("S", (), {"buffer": io.BytesIO(body)})())
    assert guard.hook.main() == 0


def test_oversized_payload_exits_2_through_the_real_hook():
    big = b'{"tool_name":"Bash","tool_input":{"command":"' + b"a" * (9 * 1024 * 1024) + b'"}}'
    proc = run_hook(big)
    assert proc.returncode == 2
    assert b"larger than" in proc.stderr
