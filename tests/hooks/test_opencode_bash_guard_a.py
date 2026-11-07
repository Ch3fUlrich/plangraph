#!/usr/bin/env python3
"""Tests for configuration/opencode/plugins/bash-guard/index.mjs.

Drives the OpenCode bash-guard plugin through a minimal fake ctx harness
under Node.js, verifying:
- Incident-shape command denial (unquoted heredoc with backticks)
- Claude background invocation denial (--bg with $() in double quotes)
- Safe command allow paths (quoted heredoc, plain commands, echo with <<)
- Forward-compat tool naming ('shell' and 'bash' intercepted; others bypassed)
- Resilient fail-open behavior (missing guard, non-zero guard exit, timeout)
- Orchestrator-role denials carry the canary's "bash-guard: DENIED" marker (D-665)
- Absence of embedded rule logic in the plugin source

This file holds TestOpenCodeBashGuard (plugin basics, the orchestrator role
and D-665); the L2 read-only role continues in test_opencode_bash_guard_b.py
and test_opencode_bash_guard_c.py. The Node harness and the plugin/hook path
resolution live in _helpers.py. Run directly:

    python3 tests/hooks/test_opencode_bash_guard_a.py
"""
import tempfile
import unittest
from pathlib import Path

from _helpers import (DENIED_MARKER, INCIDENT_COMMAND,  # noqa: F401
                      PLUGIN_PATH, ROOT, run_plugin)

@unittest.skipUnless(PLUGIN_PATH.exists(),
                     "bash-guard plugin not present at %s" % PLUGIN_PATH)
class TestOpenCodeBashGuard(unittest.TestCase):

    def test_incident_shape_unquoted_heredoc_denied(self):
        cmd = "cat <<EOF\nIf this fails, run:\n`netplan apply`\nEOF\n"
        res = run_plugin({"tool": "shell", "input": {"command": cmd}})
        self.assertFalse(res["allowed"], "Incident shape must be denied")
        self.assertIsNotNone(res["error"])
        self.assertIn("heredoc", res["error"].lower())

    def test_quoted_heredoc_allowed(self):
        cmd = "cat <<'EOF'\nIf this fails, run:\n`netplan apply`\nEOF\n"
        res = run_plugin({"tool": "shell", "input": {"command": cmd}})
        self.assertTrue(res["allowed"], f"Quoted heredoc must be allowed: {res['error']}")
        self.assertIsNone(res["error"])

    def test_plain_command_allowed(self):
        cmd = "ls -la"
        res = run_plugin({"tool": "shell", "input": {"command": cmd}})
        self.assertTrue(res["allowed"], f"Plain command must be allowed: {res['error']}")
        self.assertIsNone(res["error"])

    def test_echo_with_redirection_symbols_allowed(self):
        cmd = 'echo "a << b"'
        res = run_plugin({"tool": "shell", "input": {"command": cmd}})
        self.assertTrue(res["allowed"], f"Echo with << must be allowed: {res['error']}")
        self.assertIsNone(res["error"])

    def test_claude_bg_command_substitution_denied(self):
        cmd = 'claude --bg "$(cat f)"'
        res = run_plugin({"tool": "shell", "input": {"command": cmd}})
        self.assertFalse(res["allowed"], "claude --bg with $() must be denied")
        self.assertIsNotNone(res["error"])
        self.assertIn("claude", res["error"].lower())

    def test_repo_root_derived_from_plugin_location(self):
        """Without AUTOOS_REPO_ROOT the plugin finds tools/hooks/bash_guard.py four levels above itself."""
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / "tools" / "hooks").mkdir(parents=True)
            (tree / "configuration" / "opencode" / "plugins" / "bash-guard").mkdir(parents=True)
            (tree / "tools" / "hooks" / "bash_guard.py").write_text(
                (ROOT / "tools" / "hooks" / "bash_guard.py").read_text(encoding="utf-8"), encoding="utf-8")
            plugin = tree / "configuration" / "opencode" / "plugins" / "bash-guard" / "index.mjs"
            plugin.write_text(PLUGIN_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            cmd = "cat <<EOF\n`netplan apply`\nEOF"
            res = run_plugin({"tool": "shell", "input": {"command": cmd}},
                             env_overrides={"AUTOOS_REPO_ROOT": None}, plugin_path=plugin, keep_location=True)
            self.assertFalse(res["allowed"], "the incident shape must be refused via the location-derived root")

    def test_guard_missing_fail_open_and_logged(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            res = run_plugin(
                {"tool": "shell", "input": {"command": "ls -la"}},
                env_overrides={"AUTOOS_REPO_ROOT": empty_dir},
            )
            self.assertTrue(res["allowed"], "Missing guard script must fail open")
            guard_lines = [l for l in res["stderr"].splitlines() if l.startswith("bash-guard:")]
            self.assertEqual(len(guard_lines), 1, f"Expected 1 log line, got: {res['stderr']}")
            self.assertIn("missing", guard_lines[0].lower())

    def test_guard_timeout_allowed_and_logged(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            guard = Path(tmp_root) / "tools" / "hooks" / "bash_guard.py"
            guard.parent.mkdir(parents=True, exist_ok=True)
            guard.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

            res = run_plugin(
                {"tool": "shell", "input": {"command": "ls -la"}},
                env_overrides={"AUTOOS_REPO_ROOT": tmp_root},
                timeout=15,
            )
            self.assertTrue(res["allowed"], "Timeout must allow")
            self.assertLess(res["elapsed"], 8.0, f"Expected under 8s elapsed, got {res['elapsed']:.2f}s")
            self.assertGreaterEqual(res["elapsed"], 4.5, f"Expected at least 4.5s elapsed, got {res['elapsed']:.2f}s")
            guard_lines = [l for l in res["stderr"].splitlines() if l.startswith("bash-guard:")]
            self.assertEqual(len(guard_lines), 1, f"Expected 1 log line, got: {res['stderr']}")
            self.assertIn("timeout", guard_lines[0].lower())

    def test_guard_exit_3_allowed_and_logged(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            guard = Path(tmp_root) / "tools" / "hooks" / "bash_guard.py"
            guard.parent.mkdir(parents=True, exist_ok=True)
            guard.write_text("import sys\nsys.exit(3)\n", encoding="utf-8")

            res = run_plugin(
                {"tool": "shell", "input": {"command": "ls -la"}},
                env_overrides={"AUTOOS_REPO_ROOT": tmp_root},
            )
            self.assertTrue(res["allowed"], "Non-zero (3) guard exit must fail open")
            guard_lines = [l for l in res["stderr"].splitlines() if l.startswith("bash-guard:")]
            self.assertEqual(len(guard_lines), 1, f"Expected 1 log line, got: {res['stderr']}")
            self.assertIn("3", guard_lines[0])

    def test_tool_names_shell_and_bash_checked_others_ignored(self):
        with tempfile.TemporaryDirectory() as tmp_root:
            guard = Path(tmp_root) / "tools" / "hooks" / "bash_guard.py"
            guard.parent.mkdir(parents=True, exist_ok=True)
            marker = guard.parent / "marker.txt"
            guard.write_text(
                "import sys\n"
                "from pathlib import Path\n"
                "Path(sys.argv[0]).parent.joinpath('marker.txt').write_text('ran', encoding='utf-8')\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )

            # 1. Unrelated tool name -> ignored without spawning
            res_other = run_plugin(
                {"tool": "python", "input": {"command": "print('hello')"}},
                env_overrides={"AUTOOS_REPO_ROOT": tmp_root},
            )
            self.assertTrue(res_other["allowed"])
            self.assertFalse(marker.exists(), "Unrelated tool must not spawn guard")

            # 2. 'shell' tool -> spawned
            res_shell = run_plugin(
                {"tool": "shell", "input": {"command": "ls"}},
                env_overrides={"AUTOOS_REPO_ROOT": tmp_root},
            )
            self.assertTrue(res_shell["allowed"])
            self.assertTrue(marker.exists(), "'shell' tool must spawn guard")

            # 3. 'bash' tool -> spawned
            marker.unlink()
            res_bash = run_plugin(
                {"tool": "bash", "input": {"command": "ls"}},
                env_overrides={"AUTOOS_REPO_ROOT": tmp_root},
            )
            self.assertTrue(res_bash["allowed"])
            self.assertTrue(marker.exists(), "'bash' tool must spawn guard")

    def test_plugin_source_contains_no_rule_logic(self):
        self.assertTrue(PLUGIN_PATH.exists(), f"Plugin missing at {PLUGIN_PATH}")
        content = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertNotIn("<<", content, "Plugin must not contain heredoc operator")
        self.assertNotIn("heredoc", content.lower(), "Plugin must not contain heredoc keyword")
        self.assertNotIn("netplan", content.lower(), "Plugin must not contain netplan rule")
        self.assertNotIn("--bg", content, "Plugin must not contain --bg rule")
        self.assertIn("bash_guard.py", content, "Plugin must reference bash_guard.py")

    # ------------------------------------------------------------------
    # Orchestrator role (AUTOOS_GUARD_ROLE=orchestrator): the guard denies
    # shell writes outside .oc-pilot/ while leaving the old behaviour
    # untouched when the role is unset.
    # ------------------------------------------------------------------

    ORCH_ENV = {"AUTOOS_GUARD_ROLE": "orchestrator"}

    def _orch_denied(self, cmd, *needles):
        res = run_plugin({"tool": "shell", "input": {"command": cmd}},
                         env_overrides=dict(self.ORCH_ENV))
        self.assertFalse(res["allowed"], f"Orchestrator must deny: {cmd!r} (error={res['error']})")
        self.assertIsNotNone(res["error"])
        for needle in needles:
            self.assertIn(needle, res["error"].lower())

    def _orch_allowed(self, cmd):
        res = run_plugin({"tool": "shell", "input": {"command": cmd}},
                         env_overrides=dict(self.ORCH_ENV))
        self.assertTrue(res["allowed"], f"Orchestrator must allow: {cmd!r} (error={res['error']})")
        self.assertIsNone(res["error"])

    def test_orch_redirect_single_outside_denied(self):
        self._orch_denied("echo hi > outside.txt", "outside.txt")

    def test_orch_redirect_double_outside_denied(self):
        self._orch_denied("echo hi >> /tmp/out.log", "/tmp/out.log")

    def test_orch_redirect_fd2_outside_denied(self):
        self._orch_denied("false 2> /tmp/err.log", "/tmp/err.log")

    def test_orch_redirect_noclobber_outside_denied(self):
        self._orch_denied("echo hi >| /tmp/f.txt", "/tmp/f.txt")

    def test_orch_redirect_ampsingle_outside_denied(self):
        self._orch_denied("cmd &> /tmp/all.log", "/tmp/all.log")

    def test_orch_redirect_quoted_double_outside_denied(self):
        self._orch_denied('echo hi > "outside.txt"', "outside.txt")

    def test_orch_redirect_quoted_single_outside_denied(self):
        self._orch_denied("echo hi > 'outside.txt'", "outside.txt")

    def test_orch_redirect_inside_pilot_allowed(self):
        self._orch_allowed("echo hi > .oc-pilot/notes.txt")
        self._orch_allowed("echo hi >> .oc-pilot/log.txt")
        self._orch_allowed("echo hi > /dev/null")

    def test_orch_tee_outside_denied(self):
        self._orch_denied("echo hi | tee /tmp/out.txt", "/tmp/out.txt")

    def test_orch_tee_inside_pilot_allowed(self):
        self._orch_allowed("echo hi | tee .oc-pilot/out.txt")

    def test_orch_sed_i_outside_denied(self):
        self._orch_denied("sed -i 's/a/b/' /etc/hosts", "/etc/hosts")
        self._orch_denied("sed -i 's/a/b/' outside.txt", "outside.txt")

    def test_orch_sed_n_read_only_allowed(self):
        self._orch_allowed("sed -n '1,5p' /etc/hosts")

    def test_orch_python_c_write_outside_denied(self):
        self._orch_denied(
            'python3 -c "open(\'/tmp/x.txt\', \'w\').write(\'hi\')"', "/tmp/x.txt")
        self._orch_denied(
            'python -c "open(\'outside.txt\', \'a\')"', "outside.txt")

    def test_orch_python_c_write_inside_pilot_allowed(self):
        self._orch_allowed('python3 -c "open(\'.oc-pilot/x.txt\', \'w\').write(\'hi\')"')
        self._orch_allowed('python3 -c "print(open(\'.oc-pilot/x.txt\').read())"')

    def test_orch_cp_mv_outside_denied(self):
        self._orch_denied("cp a.txt /tmp/b.txt", "/tmp/b.txt")
        self._orch_denied("mv a.txt /tmp/b.txt", "/tmp/b.txt")

    def test_orch_git_mutation_denied(self):
        self._orch_denied("git apply patch.diff")
        self._orch_denied("git am patch.diff")
        self._orch_denied("git commit -m x")
        self._orch_denied("git checkout -- file.txt")
        self._orch_denied("git checkout file.txt")
        self._orch_denied("git reset HEAD~1")
        self._orch_denied("git rebase main")

    def test_orch_git_read_only_and_orchestrator_allowed(self):
        self._orch_allowed("git fetch origin")
        self._orch_allowed("git log --oneline -5")
        self._orch_allowed("git show HEAD")
        self._orch_allowed("git diff")
        self._orch_allowed("git status")
        self._orch_allowed("git branch -a")
        self._orch_allowed("git cherry-pick abc123")
        self._orch_allowed("git merge --ff-only origin/main")
        self._orch_allowed("git push")

    def test_orch_read_only_commands_allowed(self):
        self._orch_allowed("cat file.txt")
        self._orch_allowed("grep -rn pattern .")
        self._orch_allowed("ls -la")

    def test_orch_tool_commands_allowed(self):
        self._orch_allowed("python3 tools/autoos-agent.py list")
        self._orch_allowed("python3 tools/autoos_gateway_key.py status")
        self._orch_allowed("python3 tools/review-call.py")

    def test_orch_role_unset_keeps_old_behaviour(self):
        # With AUTOOS_GUARD_ROLE explicitly unset, the old flow applies:
        # a plain redirect outside .oc-pilot/ is not a legacy incident shape.
        res = run_plugin({"tool": "shell", "input": {"command": "echo test > outside.txt"}},
                         env_overrides={"AUTOOS_GUARD_ROLE": None})
        self.assertTrue(res["allowed"], f"Role unset must keep old behaviour: {res['error']}")
        self.assertIsNone(res["error"])

    # ------------------------------------------------------------------
    # D-665: the c0 orchestrator-role throw runs BEFORE the python guard, so its
    # message is the only thing the L1 canary ever sees on a role denial. The
    # canary reads a denial by DENIED_MARKER alone (tools/oc_l1_canary.py), so a
    # role denial without the marker reads as "shell call inconclusive" and the
    # lane refuses forever.
    # ------------------------------------------------------------------

    def test_orchestrator_role_throw_carries_denied_marker(self):
        res = run_plugin({"tool": "shell", "input": {"command": "echo hi > outside.txt"}},
                         env_overrides=dict(self.ORCH_ENV))
        self.assertFalse(res["allowed"], f"role must deny: {res['error']}")
        self.assertIn(DENIED_MARKER, res["error"],
                      "the canary matches DENIED_MARKER only")
        self.assertIn("outside.txt", res["error"],
                      "prefixing must keep the reason")

    def test_orchestrator_role_canary_incident_carries_denied_marker(self):
        for cmd in (INCIDENT_COMMAND,
                    INCIDENT_COMMAND + "\n",
                    "echo hi > outside.txt\n" + INCIDENT_COMMAND):
            res = run_plugin({"tool": "shell", "input": {"command": cmd}},
                             env_overrides=dict(self.ORCH_ENV))
            self.assertFalse(res["allowed"], f"role must deny the incident shape: {cmd!r}")
            self.assertIn(DENIED_MARKER, res["error"], f"marker missing for {cmd!r}")


if __name__ == "__main__":
    unittest.main()
