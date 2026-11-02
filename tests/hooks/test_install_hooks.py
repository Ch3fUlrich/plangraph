#!/usr/bin/env python3
"""Tests for tools/hooks/install_hooks.sh (copies the hook scripts into a user's tree).

The installer copies every `*.py` next to it into DEST, writes a SHA256SUMS manifest and refuses a DEST that is
the checkout it ships from, the read-only AutoOS tree, or anything beneath either (H4). Its usage text documents
how to register a hook: through the exit-1 wrapper, because a missing script must never exit 2 (H3).
Each run uses a stand-in checkout of copies under a temp dir and a temp HOME, so a refusal that failed would
still touch nothing real.

    python3 tests/hooks/test_install_hooks.py
"""
import hashlib
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hookkit import HOOKS, ROOT, HookCase  # noqa: E402

SH = shutil.which("sh")
ENTRY_POINTS = ("session_title", "context_guard", "ctx_guard_session", "fleet_inject")


@unittest.skipUnless(SH and os.name != "nt", "the installer needs a POSIX sh")
class InstallerCase(HookCase):
    def setUp(self):
        super().setUp()
        self.checkout = self.tmp / "checkout"  # stand-in for a plangraph checkout
        self.hooks = self.checkout / "tools" / "hooks"
        self.hooks.mkdir(parents=True)
        for name in [p.name for p in HOOKS.glob("*.py")] + ["install_hooks.sh", "pre-push"]:
            shutil.copy(HOOKS / name, self.hooks / name)
        self.installer = self.hooks / "install_hooks.sh"
        self.dest = self.tmp / "dest"

    def install(self, *args, dest=None, path=None, installer=None, **env):
        e = self.env(**env)
        if dest is not None:
            e["DEST"] = str(dest)
        if path is not None:
            e["PATH"] = str(path)
        return subprocess.run([SH, str(installer or self.installer), *args], capture_output=True, text=True,
                              env=e, cwd=str(self.tmp), timeout=60)

    def shipped(self):
        return sorted(p.name for p in self.hooks.glob("*.py"))

    def assert_refused(self, proc, dest):
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("refusing DEST", proc.stderr)
        self.assertEqual(proc.stdout, "", "a refusal installs and lists nothing")
        self.assertFalse((Path(dest) / "SHA256SUMS").exists(), "nothing is written into a refused DEST")


class Install(InstallerCase):
    def test_copies_every_py_and_writes_the_manifest(self):
        proc = self.install(dest=self.dest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        *listed, manifest = proc.stdout.splitlines()
        self.assertEqual(manifest, str(self.dest / "SHA256SUMS"), "the manifest is listed last")
        self.assertEqual(sorted(listed), [str(self.dest / n) for n in self.shipped()])
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), sorted(self.shipped() + ["SHA256SUMS"]),
                         "only *.py is shipped: no installer, no pre-push hook")
        digests = dict(line.split("  ", 1)[::-1] for line in (self.dest / "SHA256SUMS").read_text().splitlines())
        self.assertEqual(sorted(digests), self.shipped())
        for name, digest in digests.items():
            self.assertEqual(digest, hashlib.sha256((self.hooks / name).read_bytes()).hexdigest(), name)

    def test_second_run_replaces_files_and_keeps_the_manifest_to_this_runs_files(self):
        self.install(dest=self.dest)
        (self.dest / "stale.py").write_text("# left over\n", encoding="utf-8")
        (self.dest / "hook_common.py").write_text("# edited\n", encoding="utf-8")
        proc = self.install(dest=self.dest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((self.dest / "hook_common.py").read_bytes(), (self.hooks / "hook_common.py").read_bytes())
        self.assertNotIn("stale.py", (self.dest / "SHA256SUMS").read_text())
        self.assertTrue((self.dest / "stale.py").exists(), "a file the installer did not ship is not deleted")

    def test_default_dest_is_under_home(self):
        proc = self.install()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.home / ".local" / "share" / "plangraph" / "hooks" / "SHA256SUMS").is_file())

    def test_relative_dest_is_resolved_against_the_cwd(self):
        proc = self.install(dest="rel/dest")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.tmp / "rel" / "dest" / "SHA256SUMS").is_file())

    def test_no_home_and_no_dest_is_an_error(self):
        proc = self.install(HOME=None)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("set HOME or DEST", proc.stderr)

    def test_a_dir_without_hooks_is_an_error(self):
        bare = self.tmp / "bare" / "tools" / "hooks"
        bare.mkdir(parents=True)
        shutil.copy(self.installer, bare)
        proc = self.install(dest=self.dest, installer=bare / "install_hooks.sh")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no .py hooks found", proc.stderr)

    def test_installed_hooks_run_from_the_install_dir(self):
        self.assertEqual(self.install(dest=self.dest).returncode, 0)
        for name in ENTRY_POINTS:
            proc = subprocess.run([sys.executable, str(self.dest / (name + ".py"))], input="{}", capture_output=True,
                                  text=True, env=self.env(), timeout=30)
            self.assertEqual((proc.returncode, proc.stdout), (0, ""), "%s: %s" % (name, proc.stderr))


class Refusals(InstallerCase):
    """H4: the installer refuses an AutoOS-tree DEST and a DEST inside the checkout."""

    def test_h4_refuses_a_dest_inside_the_checkout(self):
        for rel in ("", "tools/hooks", "tools/hooks/sub", "build/out", "tools/../tools/hooks/x", "a/./b/../c"):
            dest = os.path.join(str(self.checkout), rel) if rel else str(self.checkout)
            self.assert_refused(self.install(dest=dest), os.path.normpath(dest))
        for made in ("build", "a", "tools/hooks/sub", "tools/hooks/x"):
            self.assertFalse((self.checkout / made).exists(), "a refused DEST is not created: %s" % made)

    def test_h4_the_real_script_refuses_its_own_checkout(self):
        """The `tools/hooks/../..` derivation is checked against the real layout, not only the stand-in's."""
        for rel in ("never-installed", "tools/hooks/never-installed"):
            dest = ROOT / rel
            self.addCleanup(shutil.rmtree, str(dest), True)  # only if a regression let the install through
            self.assert_refused(self.install(dest=dest, installer=HOOKS / "install_hooks.sh"), dest)
            self.assertFalse(dest.exists())

    def test_h4_refuses_the_autoos_tree_under_home(self):
        autoos = self.home / "code" / "AutoOS"
        for rel in ("", "tools/hooks", "deep/er/still"):
            dest = autoos / rel
            self.assert_refused(self.install(dest=dest), dest)
            self.assertFalse(dest.exists(), "a refused DEST is not created: %s" % dest)

    def test_h4_refuses_the_autoos_tree_named_by_the_environment(self):
        elsewhere = self.tmp / "elsewhere" / "AutoOS"
        dest = elsewhere / "tools" / "hooks"
        self.assert_refused(self.install(dest=dest, AUTOOS_ROOT=elsewhere), dest)
        self.assertFalse(dest.exists())
        self.assertEqual(self.install(dest=dest).returncode, 0, "without AUTOOS_ROOT this is an ordinary DEST")

    def test_h4_refuses_through_a_symlink(self):
        real = self.tmp / "real-autoos"
        real.mkdir()
        (self.home / "code").mkdir()
        try:
            (self.home / "code" / "AutoOS").symlink_to(real)
            (self.tmp / "to-checkout").symlink_to(self.checkout)
        except OSError:
            self.skipTest("symlinks are not available")
        for dest in (real / "tools" / "hooks", self.home / "code" / "AutoOS" / "x", self.tmp / "to-checkout" / "x"):
            self.assert_refused(self.install(dest=dest), dest)

    def test_h4_a_name_sharing_the_prefix_is_not_inside_the_tree(self):
        for dest in (self.home / "code" / "AutoOS-lanes" / "hooks", self.tmp / "checkout-extra" / "hooks"):
            proc = self.install(dest=dest)
            self.assertEqual(proc.returncode, 0, "%s: %s" % (dest, proc.stderr))
            self.assertTrue((dest / "SHA256SUMS").is_file())

    def test_h4_dotted_paths_cannot_get_past_the_check_without_realpath(self):
        tools = {name: shutil.which(name) for name in ("dirname", "basename", "mkdir", "cp", "sha256sum")}
        if not all(tools.values()):
            self.skipTest("coreutils are not all on PATH")
        bin_dir = self.tmp / "bin"  # a PATH with the tools the script needs and no realpath
        bin_dir.mkdir()
        for name, target in tools.items():
            (bin_dir / name).symlink_to(target)
        autoos = os.path.join(str(self.home), "code", "AutoOS")
        for dest in (autoos + "/../AutoOS/x", str(self.checkout) + "/a/../tools/hooks/x"):
            self.assert_refused(self.install(dest=dest, path=bin_dir), os.path.normpath(dest))
        fine = str(self.tmp / "ok") + "/x/../y"
        self.assertEqual(self.install(dest=fine, path=bin_dir).returncode, 0)
        self.assertTrue((self.tmp / "ok" / "y" / "SHA256SUMS").is_file())


class Usage(InstallerCase):
    """H3: the usage text carries the exit-1 wrapper a registered hook must go through."""

    WRAPPER = '''sh -c 'test -f "$1" || exit 1; exec python3 "$1"' _ DEST/<hook>.py'''

    def test_help_documents_the_wrapper_and_installs_nothing(self):
        for flag in ("--help", "-h"):
            proc = self.install(flag, dest=self.dest)
            self.assertEqual((proc.returncode, proc.stderr), (0, ""), flag)
            self.assertIn(self.WRAPPER, proc.stdout)
            flat = " ".join(proc.stdout.split())
            for phrase in ("registers nothing", "must exit 1, never 2", "exit 2 is the Claude Code block code"):
                self.assertIn(phrase, flat)
            self.assertFalse(self.dest.exists(), "--help installs nothing")

    def test_help_names_the_event_of_every_entry_point(self):
        text = self.install("--help").stdout
        for line in ("bash_guard.py", "PreToolUse", "context_guard.py", "ctx_guard_session.py", "fleet_inject.py",
                     "UserPromptSubmit", "fleet_report.py", "Stop", "session_title.py", "PostToolUse"):
            self.assertIn(line, text)

    def test_help_matches_the_comment_block_of_the_script(self):
        head = []
        for line in self.installer.read_text(encoding="utf-8").splitlines()[1:]:
            if not line.startswith("#"):
                break
            head.append(line[2:] if line.startswith("# ") else "")
        self.assertEqual(self.install("--help").stdout.splitlines(), head)

    def test_an_unknown_argument_is_refused_and_installs_nothing(self):
        for args in (("--dest=/x",), ("-x",), ("a", "b")):
            proc = self.install(*args, dest=self.dest)
            self.assertEqual(proc.returncode, 2, args)
            self.assertIn("unknown argument", proc.stderr)
            self.assertEqual(proc.stdout, "")
        self.assertFalse(self.dest.exists())


if __name__ == "__main__":
    unittest.main(verbosity=1)
