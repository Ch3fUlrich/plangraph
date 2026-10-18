"""tools/split/scrub_check.py: per-value allowances (``[[scrub.allow_value]]``) + ``--hash-hits``.

Leak-shaped strings are joined from fragments at run time, so this file passes the
same scan it tests (CI scrubs the whole tree with these rules) and no value ever
appears in the reported output either.
"""
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "split"))

import scrub_check as sc  # noqa: E402

IP_A = ".".join(["10", "20", "30", "40"])
IP_B = ".".join(["10", "9", "8", "7"])
HOME = "/ho" + "me/" + "zol" + "tan"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def allow(path, rule, value, why="reviewed synthetic fixture"):
    """A valid [[scrub.allow_value]] entry for *value* as matched text."""
    return {"path": path, "rule": rule, "value_sha256": sha(value), "why": why}


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "t"
    root.mkdir()
    return root


def test_value_allowance_suppresses_exactly_that_value(tree):
    (tree / "f.txt").write_text("addr " + IP_A + "\n")
    hits = sc.scan_tree(tree, sc.build_config())
    assert [(h.path, h.line, h.rule) for h in hits] == [("f.txt", 1, "ipv4")]
    assert sc.scan_tree(tree, sc.build_config(allow_value=[allow("f.txt", "ipv4", IP_A)])) == []


def test_value_allowance_hashes_the_whole_regex_match(tree):
    (tree / "h.txt").write_text("cd " + HOME + "/work\n")
    assert [h.rule for h in sc.scan_tree(tree, sc.build_config())] == ["home-path"]
    assert sc.scan_tree(tree, sc.build_config(allow_value=[allow("h.txt", "home-path", HOME)])) == []
    not_the_match = sc.build_config(allow_value=[allow("h.txt", "home-path", HOME.split("/")[-1])])
    assert [h.rule for h in sc.scan_tree(tree, not_the_match)] == ["home-path"]


def test_a_different_value_on_the_same_line_and_rule_still_hits(tree):
    (tree / "f.txt").write_text("addrs " + IP_A + " " + IP_B + "\n")
    assert [(h.path, h.line, h.rule) for h in sc.scan_tree(tree, sc.build_config())] == [
        ("f.txt", 1, "ipv4")]  # one hit per rule+line, as before
    one_allowed = sc.build_config(allow_value=[allow("f.txt", "ipv4", IP_A)])
    assert [(h.path, h.line, h.rule, h.sha) for h in sc.scan_tree(tree, one_allowed)] == [
        ("f.txt", 1, "ipv4", sha(IP_B))]
    both = sc.build_config(allow_value=[allow("f.txt", "ipv4", IP_A), allow("f.txt", "ipv4", IP_B)])
    assert sc.scan_tree(tree, both) == []


def test_wrong_rule_path_or_hash_still_hits(tree):
    (tree / "f.txt").write_text("addr " + IP_A + "\n")
    for cfg in (sc.build_config(allow_value=[allow("f.txt", "email", IP_A)]),
                sc.build_config(allow_value=[allow("other/f.txt", "ipv4", IP_A)]),
                sc.build_config(allow_value=[allow("f.txt", "ipv4", IP_B)])):
        assert [(h.path, h.line, h.rule) for h in sc.scan_tree(tree, cfg)] == [("f.txt", 1, "ipv4")]


def test_path_wide_allow_and_value_allow_combine(tree):
    (tree / "f.txt").write_text("addrs " + IP_A + " " + IP_B + "\n")
    cfg = sc.build_config(allow=[{"path": "*.txt", "rules": ["ipv6"]}],
                          allow_value=[allow("f.txt", "ipv4", IP_A), allow("f.txt", "ipv4", IP_B)])
    assert sc.scan_tree(tree, cfg) == []


@pytest.mark.parametrize("entry", [
    pytest.param(lambda e: {k: v for k, v in e.items() if k != "path"}, id="missing-path"),
    pytest.param(lambda e: {k: v for k, v in e.items() if k != "rule"}, id="missing-rule"),
    pytest.param(lambda e: {k: v for k, v in e.items() if k != "value_sha256"}, id="missing-hash"),
    pytest.param(lambda e: {k: v for k, v in e.items() if k != "why"}, id="missing-why"),
    pytest.param(lambda e: {**e, "path": ""}, id="empty-path"),
    pytest.param(lambda e: {**e, "rule": "  "}, id="blank-rule"),
    pytest.param(lambda e: {**e, "why": ""}, id="empty-why"),
    pytest.param(lambda e: {**e, "path": "infra/**"}, id="glob-path"),
    pytest.param(lambda e: {**e, "path": "infra/host?.txt"}, id="question-glob"),
    pytest.param(lambda e: {**e, "rule": "ip-address"}, id="unknown-rule"),
    pytest.param(lambda e: {**e, "rule": ["ipv4"]}, id="rule-not-one-id"),
    pytest.param(lambda e: {**e, "value_sha256": e["value_sha256"][:63]}, id="short-hash"),
    pytest.param(lambda e: {**e, "value_sha256": "z" * 64}, id="non-hex-hash"),
    pytest.param(lambda e: {**e, "value_sha256": 64}, id="hash-not-a-string"),
    pytest.param(lambda e: ["ipv4"], id="entry-not-a-table"),
])
def test_malformed_allow_value_raises_without_echoing_anything(entry):
    e = allow("infra/host.txt", "ipv4", IP_A)
    with pytest.raises(ValueError) as exc:
        sc.build_config(allow_value=[entry(e)])
    msg = str(exc.value)
    assert "allow_value entry" in msg
    for secret in (IP_A, "host.txt", "infra", e["value_sha256"][:8], "reviewed"):
        assert secret not in msg


def test_rules_file_accepts_allow_value_and_cli_reports_hash_hits(tree, tmp_path, capsys):
    (tree / "f.txt").write_text("addrs " + IP_A + " " + IP_B + "\n")
    first, other = min(sha(IP_A), sha(IP_B)), max(sha(IP_A), sha(IP_B))
    rules = tmp_path / "rules.toml"

    def write_rules(*digests):
        body = '[scrub]\n[[scrub.allow]]\npath = "*.md"\nrules = ["ipv6"]\n'
        for d in digests:
            body += ('[[scrub.allow_value]]\npath = "f.txt"\nrule = "ipv4"\n'
                     'value_sha256 = "' + d + '"\nwhy = "reviewed fixture"\n')
        rules.write_text(body)

    assert sc.main([str(tree), "--hash-hits"]) == 1
    out = capsys.readouterr().out
    assert f"f.txt:1: ipv4 sha256={first}" in out       # the hash to copy into an allowance
    assert f"sha256={other}" not in out                 # one hash per hit, not every value
    for value in (IP_A, IP_B):
        assert value not in out                         # never the value itself
    write_rules(first)                                  # allowances written without a leak
    assert sc.main([str(tree), "--hash-hits", "--rules", str(rules)]) == 1
    assert f"f.txt:1: ipv4 sha256={other}" in capsys.readouterr().out
    write_rules(first, other)
    assert sc.main([str(tree), "--hash-hits", "--rules", str(rules)]) == 0



def test_output_format_is_unchanged_without_the_flag(tree, capsys):
    (tree / "f.txt").write_text("addr " + IP_A + "\n")
    assert sc.main([str(tree)]) == 1
    out = capsys.readouterr().out
    assert "f.txt:1: ipv4" in out and "sha256=" not in out
    assert "hit(s) in" in out


def test_hash_hits_never_prints_a_value_for_a_content_hit(tree, capsys):
    (tree / "h.txt").write_text("cd " + HOME + "/work\n")
    assert sc.main([str(tree), "--hash-hits"]) == 1
    out = capsys.readouterr().out
    assert f"h.txt:1: home-path sha256={sha(HOME)}" in out
    assert "zol" + "tan" not in out and HOME not in out


def test_symlink_hits_are_never_allowable_and_carry_no_hash(tree, capsys):
    (tree / "l").symlink_to("/etc/" + "hostname")
    cfg = sc.build_config(allow=[{"path": "l", "rules": ["symlink"]}],
                          allow_value=[allow("l", "symlink", "l")])
    assert [(h.path, h.line, h.rule, h.sha) for h in sc.scan_tree(tree, cfg)] == [
        ("l", 0, "symlink", "")]
    assert sc.main([str(tree), "--hash-hits"]) == 1
    out = capsys.readouterr().out
    assert "l:0: symlink" in out and "sha256=" not in out


def test_path_private_word_hit_is_value_allowable_by_the_relative_path(tree, tmp_path, capsys):
    named = "Project" + "Mondrian" + ".txt"
    (tree / named).write_text("clean\n")
    words = tmp_path / "w.txt"
    words.write_text("Project" + "Mondrian" + "\n")
    assert sc.main([str(tree), "--words", str(words), "--hash-hits"]) == 1
    out = capsys.readouterr().out
    assert named not in out and "<path-hash:" in out       # the path itself stays hidden
    assert f"private-word sha256={sha(named)}" in out      # its value is the relative path
    rules = tmp_path / "rules.toml"
    rules.write_text('[[scrub.allow_value]]\npath = "' + named + '"\nrule = "private-word"\n'
                     'value_sha256 = "' + sha(named) + '"\nwhy = "renamed fixture"\n')
    assert sc.main([str(tree), "--words", str(words), "--rules", str(rules)]) == 0
    assert "0 hit(s)" in capsys.readouterr().out


def test_scan_line_still_returns_only_rule_names(tree):
    cfg = sc.build_config()
    assert sc.scan_line("addr " + IP_A, cfg, lambda t: []) == {"ipv4"}
    assert sc.scan_line("nothing to see", cfg, lambda t: []) == set()
