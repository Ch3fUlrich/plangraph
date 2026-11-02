"""Hooks, fix round r2: bounded reads, a symlink-proof installer without realpath, a counter that never repeats.

    python3 tests/hooks/test_hooks_r2.py
"""
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hookkit import HOOKS, HookCase, load_module  # noqa: E402
from test_install_hooks import InstallerCase  # noqa: E402

BIG = "x" * (9 * 1024 * 1024)  # over the 8 MiB stdin cap


class BoundedReads(HookCase):
    def test_every_stdin_hook_reads_a_bounded_payload(self):
        for name in ("session_title", "context_guard", "fleet_report", "fleet_inject", "ctx_guard_session"):
            text = (HOOKS / (name + ".py")).read_text(encoding="utf-8")
            self.assertNotIn("sys.stdin.read()", text, name)

    def test_oversized_payload_is_ignored_by_the_silent_hooks(self):
        for name in ("session_title", "context_guard", "fleet_report", "fleet_inject", "ctx_guard_session"):
            proc = self.run_hook(HOOKS / (name + ".py"), '{"session_id": "s", "pad": "%s"}' % BIG)
            self.assertEqual((proc.returncode, proc.stdout), (0, ""), name)

    def test_helpers_stop_at_the_cap_plus_one(self):
        common = load_module("hook_common")
        asked = []

        class Stream:
            def read(self, n):
                asked.append(n)
                return b"a" * n

        with mock.patch.object(common.sys, "stdin", type("S", (), {"buffer": Stream()})()):
            with self.assertRaises(common.TooLarge):
                common.read_stdin_text(100)
        self.assertEqual(asked, [101])
        big = self.tmp / "big.json"
        big.write_text("y" * 50, encoding="utf-8")
        with self.assertRaises(common.TooLarge):
            common.read_text_capped(big, 49)
        self.assertEqual(common.read_text_capped(big, 50), "y" * 50)


@unittest.skipUnless(shutil.which("sh") and os.name != "nt", "needs POSIX sh")
class SymlinkedDest(InstallerCase):
    def no_realpath_path(self):
        tools = {n: shutil.which(n) for n in ("dirname", "basename", "mkdir", "cp", "sha256sum")}
        if not all(tools.values()):
            self.skipTest("coreutils are not all on PATH")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, target in tools.items():
            (bin_dir / name).symlink_to(target)
        return bin_dir

    def test_symlink_into_checkout_or_autoos_is_refused_without_realpath(self):
        bin_dir = self.no_realpath_path()
        autoos = self.home / "code" / "AutoOS"
        autoos.mkdir(parents=True)
        for target in (self.checkout / "tools", autoos):
            link = self.tmp / ("link-" + target.name)
            link.symlink_to(target)
            for dest in (str(link) + "/x", str(link) + "/a/b", str(link)):
                proc = self.install(dest=dest, path=bin_dir)
                self.assertEqual(proc.returncode, 1, (dest, proc.stderr))
                self.assertIn("refusing DEST", proc.stderr)

    def test_symlink_elsewhere_still_installs_without_realpath(self):
        bin_dir = self.no_realpath_path()
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "okl"
        link.symlink_to(real)
        self.assertEqual(self.install(dest=str(link) + "/d", path=bin_dir).returncode, 0)
        self.assertTrue((real / "d" / "SHA256SUMS").is_file())


class CounterRecovery(HookCase):
    def test_garbled_counter_hit_twice_in_one_second_does_not_repeat(self):
        mod = load_module("session_title")
        seq_dir = self.tmp / "routing"
        seq_dir.mkdir()
        self.patch_env(ROUTING_STATE_DIR=seq_dir)
        seen = []
        with mock.patch.object(mod.time, "time", return_value=1730000000.0):
            for _ in range(3):
                (seq_dir / "session-seq").write_text("garbled", encoding="utf-8")
                seen.append(mod.next_seq())
        self.assertEqual(seen, sorted(set(seen)), seen)
        self.assertTrue(all(1000 <= n <= 9999 for n in seen), seen)


if __name__ == "__main__":
    unittest.main()
