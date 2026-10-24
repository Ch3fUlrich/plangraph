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
        "import os\nURL = 'http://127.0.0.1:8765'\nHOME = '~/data'\n"
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


# ---- seat-review fixes (L1 triage) -------------------------------------------------

def _hits(tmp_path, text):
    tree = tmp_path / "t"
    tree.mkdir(exist_ok=True)
    (tree / "f.txt").write_text(text + "\n")
    return {h.rule for h in sc.scan_tree(tree, sc.build_config())}


def test_private_word_in_file_name_is_never_printed(tmp_path, capsys):
    import hashlib
    words = tmp_path / "w.txt"
    words.write_text("Project" + "Mordor\n")
    tree = tmp_path / "t"
    (tree / "d").mkdir(parents=True)
    named = "d/Project" + "Mordor.txt"
    (tree / named).write_text("10." + "1.2.3\n")      # also a content hit in that file
    (tree / "d" / ("Project" + "Mordor.bin")).write_bytes(b"\x00\x01")
    assert sc.main([str(tree), "--words", str(words)]) == 1
    out = capsys.readouterr().out
    assert "Mordor" not in out
    digest = hashlib.sha256(named.encode()).hexdigest()[:12]
    assert f"<path-hash:{digest}>:0: private-word" in out
    assert "<path-hash:" in out and "binary file not scanned" in out
    assert sc.main([str(tree), "--words", str(words), "--resolve", digest]) == 0
    assert named in capsys.readouterr().out


def test_ipv6_compressed_forms_detected_times_not(tmp_path):
    cc, dot = ":" * 2, "."
    for ok in ("fe80" + cc + "1", "2001:db8" + cc + "1", cc + "ffff:" + dot.join("1234"),
               "addr [" + cc + "1] up", "fe80" + cc, ":".join("12345678")):
        assert "ipv6" in _hits(tmp_path, "x = " + ok), ok
    for clean in ("at 12:34:56 today", "a[" + cc + "2]", "a[" + cc + "1]", "std" + cc + "vector",
                  "db" + cc + "add", "Foo" + cc + "bar",
                  "x = y[1:2]", "ts 2026-10-02T12:34:56"):
        assert "ipv6" not in _hits(tmp_path, clean), clean


def test_entropy_variants(tmp_path):
    lower_digit = "k3j9x0q7w2m5" + "z8c1v4b6n9a2" + "s5d7f0g3h8j1"
    assert "high-entropy" in _hits(tmp_path, "key = " + lower_digit)
    assert "high-entropy" in _hits(tmp_path, "K = " + lower_digit.upper())
    sha = "0123456789abcdef" * 4
    assert "high-entropy" not in _hits(tmp_path, "sha256: " + sha)
    assert "high-entropy" in _hits(tmp_path, "secret = " + sha)
    assert "high-entropy" in _hits(tmp_path, "API_TOKEN: " + "9f86d081884c7d65" + "9a2feaa0c55ad015")
    for clean in ("name = test_keeper_credential_guard_shapes_are_masked_2",
                  "x = averyveryverylongplainlowercaseidentifier",
                  "git " + "a" * 40, "x = " + "ab12" * 12):
        assert "high-entropy" not in _hits(tmp_path, clean), clean


def test_home_path_variants(tmp_path):
    for p in ("/var/ho" + "me/zol" + "tan/x", "/Us" + "ers/zol" + "tan/x", "C:" + "\\Us" + "ers\\zol" + "tan",
              "C:" + "/Us" + "ers/zol" + "tan/x"):
        assert "home-path" in _hits(tmp_path, "p = " + p), p
    assert "home-path" not in _hits(tmp_path, "p = /var/ho" + "me/user/x")


def test_allow_globs_are_case_insensitive(tmp_path):
    tree = tmp_path / "t"
    (tree / "Vendor").mkdir(parents=True)
    (tree / "Vendor" / "a.js").write_text("v=" + ".".join("1234") + "\n")
    cfg = sc.build_config(allow=[{"path": "vendor/**", "rules": ["ipv4"]}])
    assert sc.scan_tree(tree, cfg) == []
    assert sc.glob_re("*.db", ci=True).match("a/STATE.DB") and not sc.glob_re("*.db").match("STATE.DB")


def test_symlink_hit_cannot_be_allowed(tmp_path):
    tree = tmp_path / "t"
    tree.mkdir()
    (tree / "l").symlink_to("/etc/hostname")
    cfg = sc.build_config(allow=[{"path": "l", "rules": ["symlink"]}])
    assert [h.rule for h in sc.scan_tree(tree, cfg)] == ["symlink"]


def test_word_list_case_insensitive_whole_token_and_bad_regex_line(tmp_path, capsys):
    w = tmp_path / "w.txt"
    w.write_text("Zan" + "dor\nre:lab-" + "(\n")
    assert sc.main([str(tmp_path), "--words", str(w)]) == 2
    err = capsys.readouterr().err
    assert "line 2" in err and "lab-" not in err and "Zan" not in err
    w.write_text("Zan" + "dor\n")
    cfg = sc.build_config(words_file=w)
    hit = lambda s: "private-word" in sc.scan_line(s, cfg, lambda t: [])  # noqa: E731
    assert hit("a ZAN" + "DOR b") and hit("zan" + "dor_x") and hit("x-zan" + "dor") and hit("é zan" + "dor.")
    assert not hit("xzan" + "dor") and not hit("zan" + "dorx") and not hit("zan" + "dor9") and not hit("éZan" + "dor")
