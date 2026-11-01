#!/usr/bin/env python3
"""FLEET-HOOKS (P0), continued: the second half of the CASES table of
tests/test_bash_guard.py - round-2 wrappers, `flag=` spellings, clustered
short flags, heredoc body escapes, recursive `shell -c` scans, and the
round-2.2 cluster/sudo/`=` sweeps - plus BashGuardPayloadShapeTests: the hook
contract around the Bash-only filter and fail-open parsing. One test per
CASES row is generated as in test_bash_guard_a.py; the runners and the
per-row test factory live in _helpers.py. Run directly:

    python3 tests/hooks/test_bash_guard_b.py
"""
import json
import unittest

from _helpers import _make_case_test, run_bash, run_hook, run_hook_bytes, run_hook_raw

# (name, command, expect_deny, expect_substring_in_stderr_or_None)
CASES = [
    # ─── round 2: clustered short flags (item 3) ─────────────────────
    ("claude_clustered_short_flags_denied",
     'claude -pq "text `id`"',
     True, "claude"),
    ("claude_clustered_short_flags_allows",
     'claude -pq "plain text"',
     False, None),
    ("claude_cluster_pq_denied",
     'claude -pq "backtick `id`"',
     True, "claude"),
    ("claude_cluster_cp_denied",
     'claude -cp "backtick `id`"',
     True, "claude"),
    ("claude_cluster_cp_allows",
     'claude -cp "plain text"',
     False, None),
    ("claude_cluster_pc_denied",
     'claude -pc "backtick `id`"',
     True, "claude"),
    ("claude_cluster_pc_allows",
     'claude -pc "plain text"',
     False, None),

    # ─── round 2: heredoc body escapes (item 4) ──────────────────────
    ("heredoc_escaped_backtick_allows",
     'cat <<EOF\n\\`id\\`\nEOF\n',
     False, None),
    ("heredoc_escaped_dollar_paren_allows",
     'cat <<EOF\nuse \\$(whoami) literally\nEOF\n',
     False, None),
    ("heredoc_escaped_backslash_live_backtick_denied",
     'cat <<EOF\n\\\\`id`\nEOF\n',
     True, "heredoc"),

    # ─── round 2: recursive shell -c scan (item 5) ───────────────────
    ("bash_c_claude_bg_denied",
     'bash -c \'claude --bg "$(whoami)"\'',
     True, "claude"),
    ("sh_c_claude_p_denied",
     'sh -c \'claude -p "`id`"\'',
     True, "claude"),
    ("zsh_c_claude_bg_denied",
     'zsh -c \'claude --bg "$(whoami)"\'',
     True, "claude"),
    ("dash_c_claude_p_denied",
     'dash -c \'claude -p "`id`"\'',
     True, "claude"),
    ("nested_bash_c_claude_print_denied",
     'bash -c "bash -c \'claude --print=\\"$(id)\\"\'"',
     True, "claude"),
    ("sudo_bash_c_inner_escaped_denied",
     'sudo bash -c "claude -p \\"`id`\\""',
     True, "claude"),
    ("bash_c_ls_dollar_paren_allows",
     'bash -c \'ls "$(pwd)"\'',
     False, None),
    ("bash_c_echo_plain_allows",
     'bash -c \'echo plain\'',
     False, None),
    ("bash_lc_claude_p_denied",
     'bash -lc \'claude -p "$(id)"\'',
     True, "claude"),
    ("bash_lc_claude_p_allows",
     'bash -lc \'claude -p "plain text"\'',
     False, None),
    ("bash_ec_claude_p_denied",
     'bash -ec \'claude -p "$(id)"\'',
     True, "claude"),
    ("bash_ec_claude_p_allows",
     'bash -ec \'claude -p "plain text"\'',
     False, None),
    ("bin_bash_c_claude_p_denied",
     '/bin/bash -c \'claude -p "$(id)"\'',
     True, "claude"),
    ("bin_bash_c_claude_p_allows",
     '/bin/bash -c \'claude -p "plain text"\'',
     False, None),
    ("usr_bin_env_bash_c_claude_p_denied",
     '/usr/bin/env bash -c \'claude -p "$(id)"\'',
     True, "claude"),
    ("usr_bin_env_bash_c_claude_p_allows",
     '/usr/bin/env bash -c \'claude -p "plain text"\'',
     False, None),

    # ─── round 2.2: clean prompt allows for -prefix, -print, -pretty ──
    ("claude_dash_prefix_clean_allows",
     'claude -prefix "plain text"',
     False, None),
    ("claude_dash_print_clean_allows",
     'claude -print "plain text"',
     False, None),
    ("claude_dash_pretty_clean_allows",
     'claude -pretty "plain text"',
     False, None),

    # ─── round 2.2: short p clusters with dirty prompt (DENY) ────────
    ("claude_cluster_pz_dirty_denied",
     'claude -pz "$(id)"',
     True, "claude"),
    ("claude_cluster_pn_dirty_denied",
     'claude -pn "$(id)"',
     True, "claude"),
    ("claude_cluster_pw_dirty_denied",
     'claude -pw "$(id)"',
     True, "claude"),
    ("claude_cluster_pe_dirty_denied",
     'claude -pe "$(id)"',
     True, "claude"),
    ("claude_cluster_pm_dirty_denied",
     'claude -pm "$(id)"',
     True, "claude"),
    ("claude_cluster_px_dirty_denied",
     'claude -px "$(id)"',
     True, "claude"),
    ("claude_cluster_np_dirty_denied",
     'claude -np "$(id)"',
     True, "claude"),
    ("claude_cluster_wp_dirty_denied",
     'claude -wp "$(id)"',
     True, "claude"),
    ("claude_cluster_dp_dirty_denied",
     'claude -dp "$(id)"',
     True, "claude"),
    ("claude_cluster_hp_dirty_denied",
     'claude -hp "$(id)"',
     True, "claude"),
    ("claude_cluster_ip_dirty_denied",
     'claude -ip "$(id)"',
     True, "claude"),
    ("claude_cluster_rp_dirty_denied",
     'claude -rp "$(id)"',
     True, "claude"),
    ("claude_cluster_vp_dirty_denied",
     'claude -vp "$(id)"',
     True, "claude"),
    ("claude_cluster_cp_dirty_denied",
     'claude -cp "$(id)"',
     True, "claude"),
    ("claude_cluster_pc_dirty_denied",
     'claude -pc "$(id)"',
     True, "claude"),
    ("claude_cluster_pq_dirty_denied",
     'claude -pq "$(id)"',
     True, "claude"),

    # ─── round 2.2: short p clusters with clean prompt (ALLOW) ───────
    ("claude_cluster_pz_clean_allows",
     'claude -pz "plain text"',
     False, None),
    ("claude_cluster_pn_clean_allows",
     'claude -pn "plain text"',
     False, None),
    ("claude_cluster_pw_clean_allows",
     'claude -pw "plain text"',
     False, None),
    ("claude_cluster_pe_clean_allows",
     'claude -pe "plain text"',
     False, None),
    ("claude_cluster_pm_clean_allows",
     'claude -pm "plain text"',
     False, None),
    ("claude_cluster_px_clean_allows",
     'claude -px "plain text"',
     False, None),
    ("claude_cluster_np_clean_allows",
     'claude -np "plain text"',
     False, None),
    ("claude_cluster_wp_clean_allows",
     'claude -wp "plain text"',
     False, None),
    ("claude_cluster_dp_clean_allows",
     'claude -dp "plain text"',
     False, None),
    ("claude_cluster_hp_clean_allows",
     'claude -hp "plain text"',
     False, None),
    ("claude_cluster_ip_clean_allows",
     'claude -ip "plain text"',
     False, None),
    ("claude_cluster_rp_clean_allows",
     'claude -rp "plain text"',
     False, None),
    ("claude_cluster_vp_clean_allows",
     'claude -vp "plain text"',
     False, None),
    ("claude_cluster_cp_clean_allows",
     'claude -cp "plain text"',
     False, None),
    ("claude_cluster_pc_clean_allows",
     'claude -pc "plain text"',
     False, None),
    ("claude_cluster_pq_clean_allows",
     'claude -pq "plain text"',
     False, None),

    # ─── round 2.2: dirty prompt without p (ALLOW) ───────────────────
    ("claude_dash_c_dollar_paren_id_allows",
     'claude -c "$(id)"',
     False, None),
    ("claude_dash_d_dirty_prompt_allows",
     'claude -d "$(id)"',
     False, None),
    ("claude_dash_h_dirty_prompt_allows",
     'claude -h "$(id)"',
     False, None),
    ("claude_dash_n_dirty_prompt_allows",
     'claude -n "$(id)"',
     False, None),
    ("claude_dash_w_dirty_prompt_allows",
     'claude -w "$(id)"',
     False, None),
    ("claude_dash_r_dirty_prompt_allows",
     'claude -r "$(id)"',
     False, None),
    ("claude_dash_v_dirty_prompt_allows",
     'claude -v "$(id)"',
     False, None),
    ("claude_dash_cdh_dirty_prompt_allows",
     'claude -cdh "$(id)"',
     False, None),

    # ─── round 2.2: sudo variants with dirty prompt (DENY) ───────────
    ("sudo_n_claude_p_dirty_denied",
     'sudo -n claude -p "$(id)"',
     True, "claude"),
    ("sudo_E_claude_p_dirty_denied",
     'sudo -E claude -p "$(id)"',
     True, "claude"),
    ("sudo_uroot_claude_p_dirty_denied",
     'sudo -uroot claude -p "$(id)"',
     True, "claude"),
    ("sudo_user_equals_root_claude_p_dirty_denied",
     'sudo --user=root claude -p "$(id)"',
     True, "claude"),
    ("sudo_u_root_claude_p_dirty_denied",
     'sudo -u root claude -p "$(id)"',
     True, "claude"),

    # ─── round 2.2: sudo variants with clean prompt (ALLOW) ──────────
    ("sudo_n_claude_p_clean_allows",
     'sudo -n claude -p "plain text"',
     False, None),
    ("sudo_E_claude_p_clean_allows",
     'sudo -E claude -p "plain text"',
     False, None),
    ("sudo_uroot_claude_p_clean_allows",
     'sudo -uroot claude -p "plain text"',
     False, None),
    ("sudo_user_equals_root_claude_p_clean_allows",
     'sudo --user=root claude -p "plain text"',
     False, None),
    ("sudo_u_root_clean_allows",
     'sudo -u root claude -p "plain text"',
     False, None),

    # ─── round 2.2: flag equals with dirty prompt (DENY) ─────────────
    ("claude_p_equals_dirty_denied",
     'claude -p="$(id)"',
     True, "claude"),
    ("claude_p_equals_clean_allows",
     'claude -p="plain text"',
     False, None),
    ("claude_print_equals_dirty_denied",
     'claude --print="$(id)"',
     True, "claude"),
]

class BashGuardCaseTests(unittest.TestCase):
    """One test per CASES row, generated below."""


for _name, _command, _deny, _substr in CASES:
    setattr(BashGuardCaseTests, "test_" + _name,
            _make_case_test(_command, _deny, _substr))


class BashGuardPayloadShapeTests(unittest.TestCase):
    """The hook contract around the Bash-only filter and fail-open parsing,
    independent of any one command's shell syntax."""

    def test_non_bash_tool_is_ignored_even_with_a_dangerous_command(self):
        r = run_hook({"tool_name": "Read",
                       "tool_input": {"command": "cat <<EOF\n`id`\nEOF\n"}})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")

    def test_malformed_json_fails_open_with_a_note(self):
        r = run_hook_raw("{not json")
        self.assertEqual(r.returncode, 0)
        self.assertIn("could not parse", r.stderr)

    def test_json_not_an_object_fails_open_with_a_note(self):
        r = run_hook_raw("[1, 2, 3]")
        self.assertEqual(r.returncode, 0)
        self.assertIn("not a JSON object", r.stderr)

    def test_bash_tool_without_tool_input_fails_open_with_a_note(self):
        r = run_hook({"tool_name": "Bash"})
        self.assertEqual(r.returncode, 0)
        self.assertIn("missing or malformed", r.stderr)

    def test_bash_tool_without_command_allows_silently(self):
        r = run_hook({"tool_name": "Bash", "tool_input": {}})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")

    def test_empty_command_allows_silently(self):
        r = run_bash("")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "")

    def test_non_utf8_stdin_fails_open_without_traceback(self):
        r = run_hook_bytes(b"\xff\xfe\x00\x80")
        self.assertEqual(r.returncode, 0)
        stderr_text = r.stderr.decode("utf-8", errors="replace")
        self.assertNotIn("Traceback", stderr_text)
        self.assertNotIn("UnicodeDecodeError", stderr_text)

    def test_invalid_utf8_byte_in_command_denied_and_allowed(self):
        dirty = b'{"tool_name":"Bash","tool_input":{"command":"claude -p \\"$(id)\\" \xff"}}'
        r_dirty = run_hook_bytes(dirty)
        self.assertEqual(r_dirty.returncode, 2)
        self.assertIn("claude", r_dirty.stderr.decode("utf-8", errors="replace"))

        clean = b'{"tool_name":"Bash","tool_input":{"command":"claude -p \\"plain prompt\\" \xff"}}'
        r_clean = run_hook_bytes(clean)
        self.assertEqual(r_clean.returncode, 0)
        self.assertEqual(r_clean.stderr, b"")

    def test_oversized_payload_fails_open_with_note(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "x" * (2 * 1024 * 1024)}})
        r = run_hook_raw(payload)
        self.assertEqual(r.returncode, 0)
        self.assertIn("1 MiB", r.stderr)

    def test_raw_nul_byte_in_json_fails_open(self):
        r = run_hook_bytes(b'{"tool_name":"Bash","tool_input":{"command":"\x00"}}')
        self.assertEqual(r.returncode, 0)
        self.assertIn("could not parse", r.stderr.decode("utf-8", errors="replace"))

    def test_shell_c_depth_4_nesting_allowed_with_note(self):
        cmd = 'bash -c "bash -c \\"bash -c \\\\\\\"bash -c \'claude -p \\\\\\\\\\\\\\\"$(id)\\\\\\\\\\\\\\\"\'\\\\\\\"\\\""'
        r = run_bash(cmd)
        self.assertEqual(r.returncode, 0)
        self.assertIn("nesting", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
