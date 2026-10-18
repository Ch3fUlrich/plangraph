"""tools/hooks/pre-push: the scrub gate refuses pushes whose changed files leak.

The hook reuses scrub_check's rules and output contract (path:line: rule, never a
matched value), so these tests only drive it as git would: ref lines on stdin,
exit code 0/1, output free of the planted strings.
"""
import os
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "tools" / "hooks" / "pre-push"
WORKFLOWS = ROOT / ".github" / "workflows" / "tests.yml"
ZERO = "0" * 40


def fake_ip():
    """IPv4 just above the RFC 5737 doc range: obviously fake, built at runtime so
    no address literal appears in the diff (scrub_check allow-lists 192.0.2.x
    itself, so a documented address would not fire)."""
    return ".".join(str(o + (1 if o != 2 else 0)) for o in (191, 1, 1, 0))


def fake_word():
    return "zor" + "pshot"


def fake_ip2():
    """A second ipv4 hit outside the doc allow-prefixes, built at runtime like fake_ip."""
    return ".".join(str(o) for o in (192, 5, 6, 7))


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def value_rules(path, rule, *values):
    """A rules.toml with one [[scrub.allow_value]] per *value* for (path, rule)."""
    body = "[scrub]\n"
    for v in values:
        body += ('[[scrub.allow_value]]\npath = "' + path + '"\nrule = "' + rule + '"\n'
                 'value_sha256 = "' + sha(v) + '"\nwhy = "reviewed synthetic fixture"\n')
    return body


def git(cwd, *args):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(["git", *args], cwd=str(cwd), env=env, check=True,
                          capture_output=True, text=True).stdout


def commit_file(repo, rel, text):
    p = Path(repo) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
        "commit", "-qm", "change")
    return git(repo, "rev-parse", "HEAD").strip()


def commit_symlink(repo, rel, target):
    """Make *rel* a symlink to *target*: a type change if it already existed as a
    regular file, an addition otherwise."""
    p = Path(repo) / rel
    p.unlink(missing_ok=True)
    p.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, p)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
        "commit", "-qm", "relink")
    return git(repo, "rev-parse", "HEAD").strip()


def run_hook(repo, lines, env_extra=None):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    env.pop("PLANGRAPH_SCRUB_WORDS", None)
    env.pop("PLANGRAPH_SCRUB_RULES", None)
    env.update(env_extra or {})
    stdin = "".join(l + "\n" for l in lines)
    return subprocess.run([sys.executable, str(HOOK), "origin", "https://example.invalid/r.git"],
                          cwd=str(repo), input=stdin, capture_output=True, text=True,
                          env=env, timeout=120)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "t")
    git(r, "config", "user.email", "t@example.invalid")
    base = commit_file(r, "app/clean.py", "x = 1\n")
    return r, base


def push_line(ref, sha, remote_sha):
    return f"refs/heads/{ref} {sha} refs/heads/{ref} {remote_sha}"


def test_hook_refuses_pushed_public_pattern_hit(repo):
    r, base = repo
    head = commit_file(r, "app/net.py", f"url = \"{fake_ip()}\"\n")
    res = run_hook(r, [push_line("main", head, base)])
    out = res.stdout + res.stderr
    assert res.returncode == 1, out
    assert "app/net.py:1: ipv4" in out
    assert fake_ip() not in out, "the matched value must never reach the output"


def test_hook_passes_a_clean_push(repo):
    r, base = repo
    head = commit_file(r, "app/other.py", "y = 2\n")
    res = run_hook(r, [push_line("main", head, base)])
    assert res.returncode == 0, res.stdout + res.stderr


def test_hook_refuses_a_type_change_into_a_home_path_symlink(repo):
    r, _ = repo
    remote = commit_file(r, "app/notes.txt", "plain file\n")
    head = commit_symlink(r, "app/notes.txt", "/home/" + "zoe/prod-notes.txt")
    res = run_hook(r, [push_line("main", head, remote)])
    out = res.stdout + res.stderr
    assert res.returncode == 1, out
    assert "app/notes.txt:0: symlink" in out
    assert "zoe" not in out, "the link target is the leak and must not be printed"


def test_type_change_to_an_in_repo_relative_symlink_matches_the_added_case(repo):
    """The symlink rule is blind to the target, so a T change and the equivalent
    addition are refused alike (scrub_check: a link target is never allow-listable)."""
    r, base = repo
    added = commit_symlink(r, "app/new_link.txt", "../clean.py")
    res_add = run_hook(r, [push_line("main", added, base)])
    out_add = res_add.stdout + res_add.stderr
    remote = commit_file(r, "app/old_notes.txt", "plain file\n")
    head = commit_symlink(r, "app/old_notes.txt", "../clean.py")
    res_chg = run_hook(r, [push_line("main", head, remote)])
    out_chg = res_chg.stdout + res_chg.stderr
    for res, out, rel in ((res_add, out_add, "app/new_link.txt"),
                          (res_chg, out_chg, "app/old_notes.txt")):
        assert res.returncode == 1, out
        assert f"{rel}:0: symlink" in out
        assert "../clean.py" not in out


def test_unset_scrub_words_is_graceful_with_notice(repo):
    r, base = repo
    head = commit_file(r, "app/third.py", "z = 3\n")
    res = run_hook(r, [push_line("main", head, base)])
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Traceback" not in res.stderr
    notices = [l for l in res.stderr.splitlines() if "PLANGRAPH_SCRUB_WORDS" in l]
    assert len(notices) == 1, res.stderr


def test_new_branch_diffes_against_merge_base_with_origin_main(repo, tmp_path):
    r, _ = repo
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare")
    git(r, "remote", "add", "origin", str(origin))
    git(r, "push", "-q", "origin", "main")
    head = commit_file(r, "app/leak.py", f"peer = \"{fake_ip()}\"\n")
    res = run_hook(r, [push_line("feature", head, ZERO)])
    out = res.stdout + res.stderr
    assert res.returncode == 1, out
    assert "app/leak.py:1: ipv4" in out
    assert fake_ip() not in out


def test_words_file_from_env_refuses_without_echoing_the_word(repo, tmp_path):
    r, base = repo
    words = tmp_path / "words.txt"
    words.write_text(f"# comment\n{fake_word()}\n")
    head = commit_file(r, "app/plan.py", f"plan = \"{fake_word()}\"\n")
    res = run_hook(r, [push_line("main", head, base)],
                   env_extra={"PLANGRAPH_SCRUB_WORDS": str(words)})
    out = res.stdout + res.stderr
    assert res.returncode == 1, out
    assert "app/plan.py:1: private-word" in out
    assert fake_word() not in out
    assert str(words) not in out, "the words-file path itself may leak a private name"


def test_hook_applies_an_allow_value_so_the_push_passes(repo, tmp_path):
    """CI's tree scan exempts a reviewed value by (path, rule, sha256); a push of the
    same file must pass too -- scan_blob now runs the value-aware check, not scan_line."""
    r, base = repo
    rules = tmp_path / "rules.toml"
    rules.write_text(value_rules("app/data.json", "ipv4", fake_ip()))
    head = commit_file(r, "app/data.json", '{"ip": "' + fake_ip() + '"}\n')
    res = run_hook(r, [push_line("main", head, base)],
                   env_extra={"PLANGRAPH_SCRUB_RULES": str(rules)})
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert fake_ip() not in out, "an allowed value is still never printed"


def test_hook_refuses_when_a_second_value_on_the_line_is_not_allowed(repo, tmp_path):
    r, base = repo
    rules = tmp_path / "rules.toml"
    rules.write_text(value_rules("app/data.json", "ipv4", fake_ip()))
    head = commit_file(r, "app/data.json",
                       '{"ips": ["' + fake_ip() + '", "' + fake_ip2() + '"]}\n')
    res = run_hook(r, [push_line("main", head, base)],
                   env_extra={"PLANGRAPH_SCRUB_RULES": str(rules)})
    out = res.stdout + res.stderr
    assert res.returncode == 1, out
    assert "app/data.json:1: ipv4" in out
    for value in (fake_ip(), fake_ip2()):
        assert value not in out


def test_hook_allow_value_is_bound_to_its_path(repo, tmp_path):
    """The same value at a different path is not covered by the allowance."""
    r, base = repo
    rules = tmp_path / "rules.toml"
    rules.write_text(value_rules("app/data.json", "ipv4", fake_ip()))
    head = commit_file(r, "app/other.json", '{"ip": "' + fake_ip() + '"}\n')
    res = run_hook(r, [push_line("main", head, base)],
                   env_extra={"PLANGRAPH_SCRUB_RULES": str(rules)})
    out = res.stdout + res.stderr
    assert res.returncode == 1, out
    assert "app/other.json:1: ipv4" in out
    assert fake_ip() not in out


def test_hook_value_allows_a_path_level_private_word_hit(repo, tmp_path):
    """A path-level private-word hit carries the relative path as its value, so the
    same [[scrub.allow_value]] entry that lets CI's scan_tree pass lets the push pass."""
    r, base = repo
    rel = "app/" + fake_word() + "-plan.txt"
    words = tmp_path / "words.txt"
    words.write_text(fake_word() + "\n")
    head = commit_file(r, rel, "clean\n")
    lines = [push_line("main", head, base)]

    res = run_hook(r, lines, env_extra={"PLANGRAPH_SCRUB_WORDS": str(words)})
    out = res.stdout + res.stderr
    assert res.returncode == 1, out
    assert "<path-hash:" in out
    for leak in (fake_word(), rel):
        assert leak not in out, "the hitting path is itself the leak"

    rules = tmp_path / "rules.toml"
    rules.write_text(value_rules(rel, "private-word", rel))
    res = run_hook(r, lines, env_extra={"PLANGRAPH_SCRUB_WORDS": str(words),
                                        "PLANGRAPH_SCRUB_RULES": str(rules)})
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "<path-hash:" not in out and fake_word() not in out


def test_ci_scrub_job_yaml_parses_and_calls_the_scrub():
    wf = yaml.safe_load(WORKFLOWS.read_text(encoding="utf-8"))
    jobs = wf["jobs"]
    scrub = [j for j in jobs.values()
             if any("scrub_check.py" in (s.get("run") or "") for s in j.get("steps", []))]
    assert len(scrub) == 1, "exactly one CI job must run the scrub"
    cmds = [s["run"] for s in scrub[0]["steps"] if "scrub_check.py" in (s.get("run") or "")]
    assert "--words" not in cmds[0], "CI runs public patterns only, no words list"
    assert "--rules" in cmds[0]
    assert scrub[0]["runs-on"] == jobs["pytest"]["runs-on"]
