#!/usr/bin/env python3
"""TestL2ReadOnlyRole, part 2: the Sonnet round-4 findings (backslashes,
bare heads, option values, rev:path specifiers, the widened secret list,
symlink escapes, wrapper heads), the round-5 findings (ignore-defeating and
glob rg flags, abbreviated git long options) and the L2-SECRETS gate (the
gateway key is unreadable from an L2 shell). Part 1 is
test_opencode_bash_guard_b.py; the class name and the shared role helpers
(L2RoleChecks in _helpers.py) are kept across both halves. Run directly:

    python3 tests/hooks/test_opencode_bash_guard_c.py
"""
import tempfile
import unittest
from pathlib import Path

from _helpers import DENIED_MARKER, L2RoleChecks, run_plugin  # noqa: F401


class TestL2ReadOnlyRole(L2RoleChecks, unittest.TestCase):

    # ------------------------------------------------------------------
    # Sonnet round-4 REJECT (2026-10-08), findings 1-7. Every one of them is
    # the same shape: a token the closed list read as a name on the list, while
    # the bytes the shell actually acted on were something else.
    #
    # (1) BLOCKER  `cat \<newline>/etc/passwd` — a line continuation to bash.
    #     collectSegments flattens the newline to a space (index 268) and leaves
    #     the backslash in place, so tokenizeSegment (index 408) reads `\ ` as an
    #     ESCAPED SPACE and glues it to the next word: the guard audited the word
    #     " /etc/passwd", whose leading space walked past the leading-"/" test at
    #     index 1047. The lane's `.env` is the same trick.
    # (2) HIGH     head was baseName(words[idx]) (index 1147), so ANY binary
    #     wearing an allow-listed name was that name: /tmp/evil/cat, ./cat.
    # (3) MED      l2OperandReason skipped every token starting with "-" (index
    #     1062), so the path rode in as an option's VALUE: rg --file=/etc/passwd,
    #     rg --ignore-file=/etc/shadow.
    # (4) MED      the secret rule matched the WHOLE token (index 1031), so git's
    #     rev:path spelling hid it: git show HEAD:.env, git show :.env.
    # (5) MED      the secret list was five patterns long and case-sensitive.
    # (6) LOW      a repo-relative NAME is not a repo-relative FILE: a symlink
    #     inside the checkout reads outside it, and rg -L resolves the link
    #     itself.
    # (7) LOW      a wrapper was skipped, not audited: `sudo cat` is a second
    #     user's `cat`, and an L2 needs no wrapper at all.
    # ------------------------------------------------------------------

    def test_f1_backslash_denied_in_any_form(self):
        for cmd in ("cat \\\n/etc/passwd", "cat \\\n.env", "ls \\\nconfig/.env",
                    "rg foo \\\n/etc/shadow", "git show \\\nHEAD:.env",
                    'cat "a\\\\b"', "rg foo\\\\bar .", "cat doc\\\\x"):
            self._l2_denied(cmd, "backslash")

    def test_f1_leading_whitespace_in_a_path_operand_denied(self):
        # The other half of finding 1: even with the flatten fixed, an operand is
        # checked stripped, so a quoted space is not a second escape hatch.
        self._l2_denied('cat " /etc/passwd"', "path outside repo")
        self._l2_denied('rg foo " /etc/shadow"', "path outside repo")
        self._l2_denied('cat " .env"', "secret")
        self._l2_denied('cat "/etc/passwd "', "path outside repo")

    def test_f2_head_must_be_a_bare_allow_listed_word(self):
        for cmd in ("/tmp/evil/cat x", "./cat x", "/usr/bin/ls", "./git status",
                    "../tools/cat README.md", "bin/cat README.md"):
            self._l2_denied(cmd, "bare")

    def test_f3_option_values_are_path_checked(self):
        for cmd in ("rg --ignore-file=/etc/shadow x", "rg --file=/etc/passwd foo",
                    "rg -f=/etc/passwd foo", "rg -f/etc/passwd foo",
                    "ls --block-size=/etc/passwd", "rg --ignore-file=.env x"):
            self._l2_denied(cmd, "path outside repo" if "/etc/" in cmd else "secret")

    def test_f3_path_naming_rg_options_denied_outright(self):
        for cmd in ("rg --ignore-file .gitignore x .", "rg --file patterns.txt x .",
                    "rg -f patterns.txt x .", "rg --path-separator=:: x ."):
            self._l2_denied(cmd, "reads a path")

    def test_f3_pattern_file_operand_still_reads_as_a_secret(self):
        # The value of a denied option is checked BEFORE the option is refused, so
        # the reason an L2 sees names the file, not the flag.
        self._l2_denied("rg -f .env foo", "secret")

    def test_f4_git_rev_path_specifiers_are_split_and_checked(self):
        for cmd in ("git show HEAD:.env", "git show :.env",
                    "git show HEAD:config/.env.local", "git show HEAD~1:keys/lane.key",
                    "git show refs/heads/main:.git-credentials", "git diff HEAD -- :.env"):
            self._l2_denied(cmd, "secret")
        self._l2_denied("git show HEAD:/etc/passwd", "path outside repo")

    def test_f5_secret_list_widened_and_case_insensitive(self):
        for cmd in ("cat .netrc", "cat .NETRC", "cat config/.ssh/config",
                    "cat id_ed25519", "cat id_ecdsa_sk", "cat .aws/credentials",
                    "cat credentials", "cat secrets.yml", "cat secrets.yaml",
                    "cat TLS/Server.Key", "cat srv.PEM", "cat vault.p12", "cat x.pfx",
                    "cat db_credentials.json", "cat auth.json", "cat .git-credentials",
                    "cat .npmrc", "cat .pypirc", "rg foo .ssh/"):
            self._l2_denied(cmd, "secret")

    def test_f5_secret_lookalikes_kept(self):
        for cmd in ("cat .gitignore", "cat .npmrc.example", "cat inventory.yml.example",
                    "cat .git/config", "cat README.md", "ls docs"):
            self._l2_allowed(cmd)

    def test_f6_rg_follow_flags_denied(self):
        for cmd in ("rg -L foo .", "rg --follow foo .", "rg -iL foo ."):
            self._l2_denied(cmd, "rg")

    def test_f6_symlink_operand_may_not_escape_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / "inside.txt").write_text("ok", encoding="utf-8")
            (tree / "link").symlink_to("inside.txt")
            (tree / "escape").symlink_to("/etc/shadow")
            (tree / "loop_a").symlink_to("loop_b")
            (tree / "loop_b").symlink_to("loop_a")
            env = dict(self.L2_ENV)
            env["AUTOOS_REPO_ROOT"] = tmp

            def run(cmd):
                return run_plugin({"tool": "shell", "input": {"command": cmd}},
                                  env_overrides=env, cwd=tmp)

            for cmd in ("cat escape", "rg foo escape", "ls escape", "git show escape"):
                res = run(cmd)
                self.assertFalse(res["allowed"], f"an escaping symlink must be denied: {cmd!r}")
                self.assertTrue((res["error"] or "").strip().startswith(DENIED_MARKER), cmd)
                self.assertIn("outside the lane checkout", res["error"], cmd)
            # unresolvable is not "allowed": ELOOP fails closed.
            res = run("cat loop_a")
            self.assertFalse(res["allowed"], "an unresolvable operand must be denied")
            self.assertIn("cannot resolve", res["error"])
            for cmd in ("cat inside.txt", "cat link", "cat nope.txt", "ls"):
                res = run(cmd)
                self.assertTrue(res["allowed"], f"an in-checkout read must be kept: {cmd!r}")
                self.assertIsNone(res["error"])

    def test_f7_wrapper_heads_denied(self):
        for cmd in ("sudo ls", "sudo -u root cat f", "doas ls", "su - root",
                    "exec ls", "command cat f", "nice ls", "timeout 5 ls", "time ls",
                    "nohup ls", "stdbuf -o0 cat f", "sudo", "timeout"):
            self._l2_denied(cmd, "wrapper")

    # ------------------------------------------------------------------
    # Sonnet round-5 REJECT (L2GATES-r4): two holes left in the L2 role.
    #
    # (1) MED-HIGH rg DISCOVERS the files it reads, so the operand rules never
    #     see them, and the ignore rules are the only thing that kept a recursive
    #     search off the gitignored secrets sitting in the checkout. Every flag
    #     that defeats them printed ./.env: `rg --hidden KEY`, `rg -uu KEY`,
    #     `rg -. KEY`, `rg --no-ignore --hidden KEY`. A glob picks the secret in
    #     by hand, so `-g/--glob/--iglob` go too.
    # (2) LOW      L2_GIT_REST_DENY was an exact-match table and git accepts an
    #     unambiguous ABBREVIATION of a long option: `--outpu`, `--ext-d`,
    #     `--textc` each walked past it into the very behaviour the table named.
    # ------------------------------------------------------------------

    def test_r5_rg_flags_that_defeat_the_ignore_rules_denied(self):
        for cmd in ("rg --hidden KEY", "rg --hidden=true KEY", "rg -. KEY",
                    "rg -u KEY", "rg -uu KEY", "rg -uuu KEY",
                    "rg --unrestricted KEY", "rg --no-ignore KEY",
                    "rg --no-ignore --hidden KEY", "rg --no-ignore-vcs KEY",
                    "rg --no-ignore-dot KEY", "rg --no-ignore-parent --no-ignore KEY",
                    "rg -iu KEY", "rg -i. KEY", "rg --hidden KEY .env",
                    "rg -zu KEY", "rg --no-ignore-files KEY"):
            self._l2_denied(cmd, "rg")

    def test_r5_rg_glob_flags_denied(self):
        for cmd in ("rg -g .env KEY", "rg --glob .env KEY", "rg --glob=!.env KEY",
                    "rg -g=*.env KEY", "rg --iglob .env KEY", "rg --iglob=*.env KEY",
                    "rg -iAg KEY", "rg foo -g '*.env'"):
            self._l2_denied(cmd, "rg")

    def test_r5_plain_rg_searches_kept(self):
        for cmd in ("rg foo", "rg -n foo src", "rg --line-number foo",
                    "rg --context 3 foo", "rg --count-matches foo",
                    "rg --sort path foo", "rg -i foo ."):
            self._l2_allowed(cmd)

    def test_r5_git_abbreviated_denied_long_options_denied(self):
        for cmd in ("git log --outp=/tmp/f", "git log --outp /tmp/f",
                    "git log --outpu=/tmp/f", "git diff --ext-d",
                    "git diff --ext-di", "git show --textc", "git show --textco",
                    "git diff --no-i a b", "git diff --no-ind a b",
                    "git --exec-p=/tmp/bins log", "git --git-di=/tmp/evil.git log",
                    "git --work-tre=/etc status", "git --pag log",
                    "git --confi=x status", "git --names status"):
            self._l2_denied(cmd, "git")

    def test_r5_git_read_only_long_options_kept(self):
        for cmd in ("git diff --no-ext-diff HEAD", "git show --no-textconv HEAD",
                    "git --no-pager log", "git log --oneline -5 --stat",
                    "git diff --name-only", "git status --short --branch",
                    "git log --no-color", "git show --format=%h"):
            self._l2_allowed(cmd)

    # ------------------------------------------------------------------
    # AO-L2-LAUNCH merge criterion e (L2-SECRETS, 2026-10-08): the L2 lane's
    # environment CARRIES `AUTOOS_OMNIROUTE_KEY`, because opencode expands the
    # rendered config's `{env:AUTOOS_OMNIROUTE_KEY}` inside the child and the lane
    # has no other way to its model. Carrying it is only safe while the lane's own
    # shell cannot READ it back: the launcher's allowlist picks which NAMES travel,
    # this gate picks which READS the session may perform, and the key sits in the
    # child's own environment either way — in `/proc/self/environ`, in `printenv`'s
    # output, and in every expansion. An allowlist of names with a hole in the read
    # list is a naming exercise, so each spelling that reaches the value is denied
    # here and the reason names the rule, not just the rejection.
    # ------------------------------------------------------------------

    def test_l2_shell_cannot_read_the_gateway_key(self):
        for cmd, why in (
                ("printenv", "environment"),
                ("printenv AUTOOS_OMNIROUTE_KEY", "environment"),
                ("env", "environment"),
                ("cat /proc/self/environ", "live process entry"),
                ("cat /proc/$PPID/environ", "expansion"),
                ("cat /proc/1234/environ", "live process entry"),
                ("rg KEY /proc/self/environ", "live process entry"),
                ("echo $AUTOOS_OMNIROUTE_KEY", "expansion"),
                ("ls ${AUTOOS_OMNIROUTE_KEY}", "expansion"),
                ("git log --format=$AUTOOS_OMNIROUTE_KEY", "expansion")):
            self._l2_denied(cmd, why)

    def test_l2_no_expansion_hides_in_a_glued_option_value(self):
        # Sonnet seat REJECT (L2SECRETS fix 10, 2026-10-08): l2OptionOperandReason
        # path-checked a glued short value (`-XVALUE`) only when the head was rg and
        # the option was one of rg's file-opening flags, so every OTHER glued value
        # went unexamined — and a `$` in one is the lane's own environment handed to
        # a read-only command. `rg -r$AUTOOS_OMNIROUTE_KEY foo README.md` printed the
        # key as a replacement string; `head -c$AUTOOS_OMNIROUTE_KEY README.md` made
        # it a byte count. Neither token is a path, so the path rule never saw them.
        # The rule is now lexical, not per-flag: no `$` survives in an L2 command.
        for cmd in (
                "rg -r$AUTOOS_OMNIROUTE_KEY foo README.md",
                "rg -r${OPENCODE_SERVER_PASSWORD} foo README.md",
                "head -c$AUTOOS_OMNIROUTE_KEY README.md",
                "tail -n$AUTOOS_OMNIROUTE_KEY README.md",
                "rg -m$AUTOOS_OMNIROUTE_KEY foo README.md",
                "git log -G$AUTOOS_OMNIROUTE_KEY",
                'rg -r"$AUTOOS_OMNIROUTE_KEY" foo README.md',
                "rg --replace=$AUTOOS_OMNIROUTE_KEY foo README.md",
                "rg -e$AUTOOS_OMNIROUTE_KEY README.md",
                "rg -A$((1)) foo README.md",
                # fix 11: the quote is not a boundary the guard honours, so a `"`
                # glued around the value - on a flag or on a plain operand - is the
                # same read. `cat "README$KEY.md"` puts the key inside a FILE NAME,
                # which no flag rule ever looks at.
                'rg -e"$AUTOOS_OMNIROUTE_KEY" README.md',
                'rg -r"${OPENCODE_SERVER_PASSWORD}" foo README.md',
                'cat "README$AUTOOS_OMNIROUTE_KEY.md"',
                'rg foo"$AUTOOS_OMNIROUTE_KEY" README.md',
                'rg --replace="$(printenv AUTOOS_OMNIROUTE_KEY)" foo README.md',
                'head -c"$((1+1))" README.md',
                "git log --format=$AUTOOS_OMNIROUTE_KEY",
                "git log --grep=${AUTOOS_OMNIROUTE_KEY}"):
            self._l2_denied(cmd, "expansion")
        # ANSI-C quoting writes the `$` as `\x24`, so the command can carry an
        # expansion with no `$` in the text at all - and the backslash that spells
        # it is what the guard reads first. Both are refused, fail closed.
        for cmd in ("rg $'\\x41' README.md",
                    "rg -r$'\\x24AUTOOS_OMNIROUTE_KEY' foo README.md"):
            self._l2_denied(cmd, "backslash")
        # A backtick is a substitution with no `$` either; the substitution scan
        # runs before any operand rule, whatever flag it rides in on.
        self._l2_denied("rg -r`printenv AUTOOS_OMNIROUTE_KEY` foo README.md",
                        "substitution")
        # `~` is expanded by the same shell, and a leading `~` reaches a home
        # directory outside the checkout whatever flag it is glued to.
        for cmd in ("head -n~/lane.key README.md", "rg -m~/x foo README.md"):
            self._l2_denied(cmd, "path outside repo")
        # The read-only list is unchanged: a flag with a plain glued value stays
        # allowed, so the rule costs the lane nothing it was already permitted.
        for cmd in ("head -n5 README.md", "tail -c20 README.md", "rg -m10 foo README.md",
                    "rg -e foo README.md", "head -c-1 README.md", "git log -Gfoo"):
            self._l2_allowed(cmd)


if __name__ == "__main__":
    unittest.main()
