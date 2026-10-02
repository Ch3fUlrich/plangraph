"""tools/split/scrub_check.py: every planted leak is caught, no value is printed.

The dirty tree is built at run time from fragments so this file itself never
contains a secret-, host- or path-shaped literal (the public tree is scanned
with the same tool).
"""
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "split"))

import scrub_check as sc  # noqa: E402

H = "".join  # join fragments


def _dirty(root: Path) -> dict:
    """Write one planted leak per file; return {relpath: rule}."""
    leaks = {
        "a_host.txt": ("see " + H(["alpha-box", ".corp", ".internal"]) + " now\n", "hostname"),
        "b_ip.txt": ("addr " + ".".join(["10", "20", "30", "40"]) + "\n", "ipv4"),
        "c_mail.txt": ("ping " + "jane.doe" + "@" + "acme-corp" + ".io" + "\n", "email"),
        "d_ghp.txt": ("t = " + "gh" + "p_" + "A1b2C3d4E5" * 3 + "abcdef" + "\n", "token-shape"),
        "e_pem.txt": ("-----BEGIN " + "RSA PRIVATE" + " KEY-----\n", "token-shape"),
        "f_home.txt": ("cd " + "/ho" + "me/" + "zoltan" + "/work\n", "home-path"),
        "g_word.txt": ("deploys to " + "Project" + "Mordor" + " daily\n", "private-word"),
        "h_entropy.txt": ("k = " + "q9Zx7LmP2vK8" + "rT4wB6nY1cD3fG5hJ0sA" + "\n", "high-entropy"),
        "i_cred.txt": ("login = " + "admin" + "/" + "hunter22xyz" + "\n", "credential-shape"),
        "j_aws.txt": ("id " + "AK" + "IA" + "ABCDEFGHIJKLMNOP" + "\n", "token-shape"),
    }
    for name, (text, _) in leaks.items():
        (root / name).write_text(text)
    return {n: r for n, (_, r) in leaks.items()}


def _cfg(tmp_path):
    words = tmp_path / "words.txt"
    words.write_text("# private words\nProjectMordor\nre:secret-lab-\\d+\n")
    return sc.build_config(words_file=words)


def test_dirty_tree_all_caught_values_never_printed(tmp_path):
    tree = tmp_path / "dirty"
    tree.mkdir()
    expect = _dirty(tree)
    cfg = _cfg(tmp_path)
    hits = sc.scan_tree(tree, cfg)
    got = {(h.path, h.rule) for h in hits}
    for name, rule in expect.items():
        assert (name, rule) in got, (name, rule, sorted(got))
    out = io.StringIO()
    sc.report(hits, out)
    text = out.getvalue()
    for secret in ("alpha-box", "zoltan", "hunter22", "ProjectMordor", "jane.doe",
                   "A1b2C3d4E5", "ABCDEFGHIJKLMNOP", "q9Zx7LmP2v", ".".join(["10", "20", "30", "40"])):
        assert secret not in text
    assert "a_host.txt:1: hostname" in text


def test_clean_tree_passes(tmp_path):
    tree = tmp_path / "clean"
    tree.mkdir()
    (tree / "ok.py").write_text(
        "import os\nURL = 'http://127.0.0.1:8787'\nHOME = '~/data'\n"
        "mail = 'noreply@example.invalid'\nsha = '" + "0123456789abcdef" * 4 + "'\n"
        "ip = '192.0.2.7'\nname = 'test_keeper_credential_guard_shapes_are_masked'\n")
    assert sc.scan_tree(tree, _cfg(tmp_path)) == []


def test_configurable_hostname_regex_and_cli_exit_codes(tmp_path, capsys):
    tree = tmp_path / "t"
    tree.mkdir()
    (tree / "x.md").write_text("run on " + "build" + "01" + "\n")
    rules = tmp_path / "rules.toml"
    rules.write_text('[scrub]\nhostname_patterns = ["build[0-9]+"]\n')
    assert sc.main([str(tree)]) == 0
    assert sc.main([str(tree), "--rules", str(rules)]) == 1
    out = capsys.readouterr().out
    assert "x.md:1: hostname" in out and "build01" not in out


def test_private_word_in_path_name(tmp_path):
    tree = tmp_path / "t"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / ("Project" + "Mordor" + ".txt")).write_text("clean\n")
    hits = sc.scan_tree(tree, _cfg(tmp_path))
    assert [(h.rule, h.line) for h in hits] == [("private-word", 0)]


def test_allow_entries_skip_rules_by_path(tmp_path):
    tree = tmp_path / "t"
    (tree / "vendor").mkdir(parents=True)
    (tree / "vendor" / "lib.js").write_text("v='" + ".".join("1234") + "' // q9Zx7LmP2vK8" + "rT4wB6nY1cD3fG5hJ0sA\n")
    cfg = sc.build_config(allow=[{"path": "vendor/**", "rules": ["ipv4", "high-entropy"]}])
    assert sc.scan_tree(tree, cfg) == []
    assert {h.rule for h in sc.scan_tree(tree, sc.build_config())} == {"ipv4", "high-entropy"}


def test_binary_files_are_skipped_and_counted(tmp_path):
    tree = tmp_path / "t"
    tree.mkdir()
    (tree / "b.bin").write_bytes(b"\x00\x01" + ".".join(["10", "9", "8", "7"]).encode())
    hits, skipped = sc.scan_tree(tree, sc.build_config(), with_skipped=True)
    assert hits == [] and skipped == ["b.bin"]


def test_skips_git_dir_and_symlink_is_a_hit(tmp_path):
    tree = tmp_path / "t"
    (tree / ".git").mkdir(parents=True)
    (tree / ".git" / "c").write_text(".".join(["10", "9", "8", "7"]) + "\n")
    (tree / "l").symlink_to("/etc/hostname")
    assert [(h.path, h.rule) for h in sc.scan_tree(tree, sc.build_config())] == [("l", "symlink")]
