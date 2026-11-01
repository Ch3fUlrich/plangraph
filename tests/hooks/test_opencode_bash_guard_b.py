#!/usr/bin/env python3
"""TestL2ReadOnlyRole, part 1: the L2 read-only role (AO-L2-LAUNCH merge
criterion 1) - the closed allow list of heads, everything it refuses, the
canary marker contract, the criterion-b global-option findings and the
round-3 findings. Part 2 (rounds 4-5 and the L2-SECRETS gate) continues in
test_opencode_bash_guard_c.py; the class name and the shared role helpers
(L2RoleChecks in _helpers.py) are kept across both halves. Run directly:

    python3 tests/hooks/test_opencode_bash_guard_b.py
"""
import tempfile
import unittest

from _helpers import (DENIED_MARKER, INCIDENT_COMMAND,  # noqa: F401
                      L2RoleChecks, run_plugin)

# ---------------------------------------------------------------------------
# AO-L2-LAUNCH merge criterion 1: the L2 read-only role (AUTOOS_GUARD_ROLE=l2).
#
# An orchestrator-L2 may inspect a tree and run read-only checks and NOTHING
# else: the shell is a closed allow list of command heads, not a write-scoped
# one. `orchestrator` scopes writes to .oc-pilot/; an L2 has no writable scope
# at all, because every change it makes is supposed to be a tier-3 spawn. So a
# redirect to .oc-pilot/ is refused here, a mutating `git` verb is refused, and
# any head that is not on the list is refused - fail closed, as the L2's whole
# contract depends on it.
# ---------------------------------------------------------------------------

class TestL2ReadOnlyRole(L2RoleChecks, unittest.TestCase):

    # (1) the allow list, verbatim
    def test_allow_list_git_heads(self):
        for cmd in ("git status", "git status --short", "git log --oneline -5",
                    "git diff", "git diff HEAD~1 -- tools", "git show HEAD"):
            self._l2_allowed(cmd)

    def test_allow_list_plain_heads(self):
        for cmd in ("ls -la", "cat file.txt", "rg pattern .", "head -5 f.txt",
                    "tail -n 20 f.log", "wc -l f.txt", "pwd"):
            self._l2_allowed(cmd)

    def test_allow_list_pipes_between_read_only_heads(self):
        self._l2_allowed("git log --oneline | head -20")
        self._l2_allowed("ls -la | wc -l")

    def test_wrapper_head_denied_even_around_a_read_only_head(self):
        # Sonnet round-4 finding 7: `timeout 5 ls` is still `ls` to the
        # orchestrator role, whose rule is a write TARGET. The L2 rule is a
        # closed list of HEADS, and a wrapper is a different process around the
        # read - `sudo cat` is root's `cat`. An L2 has no use for one.
        self._l2_denied("timeout 5 ls", "wrapper")
        self._l2_denied("nice ls", "wrapper")

    # (2) everything else is refused
    def test_redirect_denied_even_into_the_pilot_dir(self):
        self._l2_denied("echo x > f", "f")
        self._l2_denied("echo x >> f.log", "f.log")
        self._l2_denied("echo x > .oc-pilot/notes.txt", ".oc-pilot")
        self._l2_denied("false 2> /tmp/err.log", "/tmp/err.log")
        self._l2_denied("echo x >| /tmp/f.txt", "/tmp/f.txt")

    def test_git_mutating_verbs_denied(self):
        for cmd in ("git commit -m x", "git add -A", "git push", "git checkout -- file",
                    "git reset HEAD~1", "git rebase main", "git apply p.diff",
                    "git merge origin/main", "git branch -f x HEAD"):
            self._l2_denied(cmd, "git")

    def test_writing_and_spawning_heads_denied(self):
        for cmd in ("rm -rf /tmp/x", "python3 -c 'print(1)'", "curl https://example.com",
                    "mkdir d", "touch f", "tee /tmp/x", "cp a b", "mv a b", "sed -i s/a/b/ f",
                    "bash -c 'ls'", "npm install", "git", "echo x"):
            self._l2_denied(cmd)

    def test_pipe_into_a_writer_denied(self):
        self._l2_denied("ls | tee out.txt", "tee")

    def test_wrapper_head_still_audited(self):
        self._l2_denied("sudo rm -f /tmp/x", "rm")
        self._l2_denied("timeout 5 python3 -c 'x'", "python3")

    def test_command_substitution_denied(self):
        # `cat $(ls)` reads as `cat` on the list plus an unlisted command that
        # ran first: a substitution is not read-only, whatever its parts are.
        self._l2_denied("cat $(ls)", "substitution")
        self._l2_denied("ls `pwd`", "substitution")

    def test_stdin_redirection_denied(self):
        self._l2_denied("cat < f.txt", "redirection")

    # (3) the canary must still be denied under the new role, with the marker
    def test_canary_incident_denied_with_marker(self):
        for cmd in (INCIDENT_COMMAND, INCIDENT_COMMAND + "\n"):
            res = run_plugin({"tool": "shell", "input": {"command": cmd}},
                             env_overrides=dict(self.L2_ENV))
            self.assertFalse(res["allowed"], f"the probe must be denied: {cmd!r}")
            self.assertIn(DENIED_MARKER, res["error"])
            self.assertIn("l2 read-only", res["error"])

    def test_role_denies_without_the_python_guard_present(self):
        # Defense in depth: the role gate is the lane's own, so a missing
        # legacy guard must not turn the read-only shell back into a writable
        # one. The orchestrator role has the same shape of hole; this one is
        # pinned because an L2 has NO writable scope to fall back on.
        with tempfile.TemporaryDirectory() as empty_dir:
            env = dict(self.L2_ENV)
            env["AUTOOS_REPO_ROOT"] = empty_dir
            res = run_plugin({"tool": "shell", "input": {"command": "git commit -m x"}},
                             env_overrides=env)
        self.assertFalse(res["allowed"], "the role alone must deny")
        self.assertIn(DENIED_MARKER, res["error"] or "")

    # (4) the other roles are untouched by this gate
    def test_l2_rules_do_not_leak_into_the_orchestrator_role(self):
        res = run_plugin({"tool": "shell", "input": {"command": "git commit -m x"}},
                         env_overrides={"AUTOOS_GUARD_ROLE": "orchestrator"})
        self.assertFalse(res["allowed"])
        self.assertIn("orchestrator role", res["error"])
        # `cat` is read-only for both, but the orchestrator may write inside
        # .oc-pilot/ while an L2 may not write anywhere.
        self.assertTrue(run_plugin(
            {"tool": "shell", "input": {"command": "echo hi > .oc-pilot/notes.txt"}},
            env_overrides={"AUTOOS_GUARD_ROLE": "orchestrator"})["allowed"])

    def test_role_unset_keeps_old_behaviour(self):
        res = run_plugin({"tool": "shell", "input": {"command": "curl https://example.com"}},
                         env_overrides={"AUTOOS_GUARD_ROLE": None})
        self.assertTrue(res["allowed"], f"role unset must keep old behaviour: {res['error']}")
        res = run_plugin({"tool": "shell", "input": {"command": "echo x > f"}},
                         env_overrides={"AUTOOS_GUARD_ROLE": None})
        self.assertTrue(res["allowed"], f"role unset must keep old behaviour: {res['error']}")

    def test_l2_role_still_runs_the_legacy_guard(self):
        # An unquoted here-document whose body expands is denied by BOTH gates;
        # the legacy python guard runs after the role check, so the incident
        # rules cannot be dropped by a role that allows the head.
        cmd = 'ls\ncat <<EOF\n`netplan apply`\nEOF'
        res = run_plugin({"tool": "shell", "input": {"command": cmd}},
                         env_overrides={"AUTOOS_GUARD_ROLE": None})
        self.assertFalse(res["allowed"], "the legacy incident rule still applies")
        self.assertIn("heredoc", (res["error"] or "").lower())

    # ------------------------------------------------------------------
    # Sonnet final REJECT (criterion b): a read-only HEAD is not a read-only
    # command. `git status` is on the list, but `git -c core.fsmonitor='touch
    # /tmp/p' status` runs that fsmonitor as a shell; `rg` is on the list, but
    # `rg --pre 'sh -c id'` pipes every file through that program; `cat` is on
    # the list, but `/proc/self/environ` holds the lane's server password. The
    # flags that turn an inspection command into a program runner are denied
    # whatever head they hang off, and so is an environment-assignment prefix,
    # which reaches the same config knobs (`GIT_PAGER=x git log`) without any
    # flag at all.
    # ------------------------------------------------------------------

    def test_git_config_injecting_global_options_denied(self):
        for cmd in ("git -c core.fsmonitor='touch /tmp/p' status",
                    "git -c core.pager=x log",
                    "git -c gpg.format=x show HEAD",
                    "git --config-env=credential.helper=ENV:CRED_HELPER status",
                    "git -C /tmp/other-repo status",
                    "git --git-dir=/tmp/evil.git log",
                    "git --work-tree=/etc status",
                    "git --exec-path=/tmp/bins log",
                    "git -p log", "git --paginate log"):
            self._l2_denied(cmd, "git")

    def test_git_output_driving_subcommand_flags_denied(self):
        for cmd in ("git diff --ext-diff", "git show --ext-diff HEAD",
                    "git show --textconv", "git diff --textconv HEAD~1",
                    "git diff --no-index a b", "git log --output=/tmp/f",
                    "git diff --output=/tmp/patch.diff"):
            self._l2_denied(cmd, "git")

    def test_git_read_only_flags_kept(self):
        for cmd in ("git log", "git log --oneline -5", "git status --short",
                    "git diff --no-ext-diff HEAD", "git show --no-textconv HEAD",
                    "git --no-pager log", "git --no-pager diff"):
            self._l2_allowed(cmd)

    def test_rg_program_running_flags_denied(self):
        for cmd in ("rg --pre 'sh -c id' x .", "rg --pre /bin/sh x .",
                    "rg --pre=unzip x .", "rg --pre-glob '*.gz' x .",
                    "rg --hostname-bin /bin/sh .", "rg --search-zip x .",
                    "rg -z x ."):
            self._l2_denied(cmd, "rg")

    def test_rg_plain_search_kept(self):
        # `--hidden` moved to the deny list (round-5 finding 1): it makes rg read
        # the gitignored secrets in the repo. See
        # test_r5_rg_flags_that_defeat_the_ignore_rules_denied.
        for cmd in ("rg foo", "rg -i TODO .", "rg --ignore-case TODO ."):
            self._l2_allowed(cmd)

    def test_proc_per_process_reads_denied(self):
        for cmd in ("cat /proc/self/environ", "cat /proc/123/environ",
                    "cat /proc/*/environ", "head -1 /proc/self/cmdline",
                    "tail -f /proc/thread-self/status", "cat /proc/self/maps",
                    "cat proc/self/environ"):
            self._l2_denied(cmd, "proc")

    def test_absolute_paths_denied_even_when_harmless(self):
        # The rule is an ALLOW rule, not a spelling list: a path operand must be
        # repo-relative, so an absolute path is denied whatever it points at.
        # /proc/./self, //proc//self, /proc/*/environ and /proc/$$/environ each
        # walked past a deny list that named one spelling at a time.
        for cmd in ("cat /proc/cpuinfo", "cat /proc/meminfo",
                    "head -3 /proc/version", "ls /etc", "cat /etc/hosts"):
            self._l2_denied(cmd, "path outside repo")

    def test_env_assignment_prefix_denied(self):
        # `FOO=x <head>` sets the environment of a command the list approved,
        # which is how GIT_PAGER / GIT_DIR / GIT_CONFIG_ENV reach git without a
        # single git flag. The prefix is the payload; the head is a disguise.
        for cmd in ("GIT_PAGER=x git log", "FOO=1 git status",
                    "env GIT_DIR=/tmp/evil git log", "LINES=1 rg foo",
                    "git log; FOO=1 ls", "export FOO=1"):
            self._l2_denied(cmd)

    def test_wrapper_of_read_only_head_denied_whatever_it_wraps(self):
        # Sonnet round-4 finding 7: the wrapper skip that the orchestrator role
        # needs (so `sudo rm /` is audited as `rm`) is what let `sudo cat` read
        # as `cat` here. The L2 list is of bare heads.
        self._l2_denied("timeout 5 ls", "wrapper")
        self._l2_denied("nice ls", "wrapper")

    # ------------------------------------------------------------------
    # Sonnet round-3 REJECT (criterion b, again): three holes left.
    #
    # (1) `skipLeading` walks past the `env` wrapper, so a bare `env` — whose
    # whole job is printing the lane's environment, server password and gateway
    # keys included — had no head at all and passed. `printenv`/`set`/`declare`/
    # `export` are the same read with different names.
    # (2) the /proc rule named the SHAPES it had seen. Every other absolute
    # path — /proc/./self/environ, //proc//self//environ, /proc/$$/environ, a
    # globbed /proc, ~/.claude.json, ~/.config/opencode/auth.json — was simply
    # not on the list. A read head may only name a path that is inside the lane
    # checkout and cannot move after the shell expands it.
    # (3) a short-flag bundle (`rg -uz`) hid the denied `-z` inside a token the
    # exact-match flag list never saw.
    # ------------------------------------------------------------------

    def test_env_dumping_heads_denied(self):
        for cmd in ("env", "printenv", "set", "declare", "export", "declare -p",
                    "env | head -1", "env cat README.md", "env -i ls",
                    "env FOO=1 cat README.md", "sudo env printenv"):
            self._l2_denied(cmd, "environment")

    def test_paths_outside_the_lane_repo_denied(self):
        for cmd in ("cat /proc/./self/environ", "cat //proc//self//environ",
                    "cat /proc/se*", "cat /proc/*",
                    "rg foo /proc/self", "cat ~/.claude.json",
                    "cat ~/.config/opencode/auth.json", "cat ../sibling/file",
                    "cat docs/../../etc/passwd", "cat .config/../auth.json",
                    "rg foo ~/", "git diff HEAD -- /etc/passwd"):
            self._l2_denied(cmd, "path outside repo")
        # The `$$` and `$HOME` spellings of these reads are denied one rule
        # earlier now: no `$` reaches an L2 command at all, so the path rule never
        # sees an expansion to argue about (L2SECRETS fix 10).
        self._l2_denied("cat /proc/$$/environ", "expansion")
        self._l2_denied("ls $HOME", "expansion")

    def test_secret_files_inside_the_repo_still_denied(self):
        for cmd in ("cat .env", "cat config/.env.local", "cat keys/lane.key",
                    "cat srv.pem", "cat api-keys.yml", "cat credentials.json",
                    "cat db-credentials.prod.json", "rg -f .env foo"):
            self._l2_denied(cmd, "secret")

    def test_short_flag_bundles_hide_a_denied_flag(self):
        for cmd in ("rg -uz x .", "rg -zi x", "rg -iz x .", "rg -iuz foo tools/",
                    "rg -C2 -z foo"):
            self._l2_denied(cmd, "rg")

    def test_repo_relative_reads_kept(self):
        for cmd in ("cat README.md", "rg foo tools/", "git log -5",
                    "git diff HEAD~1 -- tools/x.py", "ls docs",
                    "head -3 AGENTS.md", "wc -l CHANGELOG.md", "tail -n 5 README.md",
                    "cat .gitignore", "ls -la ./docs"):
            self._l2_allowed(cmd)


if __name__ == "__main__":
    unittest.main()
