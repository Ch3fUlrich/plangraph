"""Drive tools/hooks/pre-push as Git would: ref lines on stdin, exit code 0/1.
Pin scrub_check's path:line:rule contract without exposing planted values.
"""
import os
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

FIXTURE_EMAIL = '@'.join(('123+fixture', 'users.noreply.github.com'))

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "tools" / "hooks" / "pre-push"
WORKFLOWS = ROOT / ".github" / "workflows" / "tests.yml"
ZERO = "0" * 40


def test_timeout_is_labelled(monkeypatch, capsys):
    import runpy
    hook = runpy.run_path(str(HOOK))

    def timeout():
        raise subprocess.TimeoutExpired("synthetic", 30)

    monkeypatch.setitem(hook["main"].__globals__, "push_main", timeout)
    assert hook["main"]() == 1
    assert capsys.readouterr().err == "pre-push: subprocess timeout: push refused\n"


def test_delete_only_push_refused_before_scans(tmp_path):
    result = run_hook(tmp_path, [f"(delete) {ZERO} refs/heads/old {'1' * 40}"])
    assert result.returncode == 1
    assert result.stderr == "pre-push: refused delete of refs/heads/old\n"


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
    git(repo, "-c", "user.name=t", "-c", "user.email=" + FIXTURE_EMAIL,
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
    git(repo, "-c", "user.name=t", "-c", "user.email=" + FIXTURE_EMAIL,
        "commit", "-qm", "relink")
    return git(repo, "rev-parse", "HEAD").strip()


def run_hook(repo, lines, env_extra=None, remote="origin"):
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    env["HOME"] = str(Path(repo).parent / "home")
    env.pop("PLANGRAPH_SCRUB_WORDS", None)
    env.pop("PLANGRAPH_SCRUB_RULES", None)
    env.update(env_extra or {})
    stdin = "".join(l + "\n" for l in lines)
    command = [sys.executable, str(HOOK), remote, "https://example.invalid/r.git"]
    result = subprocess.run(command,
                          cwd=str(repo), input=stdin, capture_output=True, text=True,
                          env=env, timeout=120)
    assert "preflight skipped" not in result.stderr
    return result


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "t")
    git(r, "config", "user.email", FIXTURE_EMAIL)
    commit_file(r, "tools/affected_tests.py", "raise SystemExit(0)\n")
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


@pytest.mark.parametrize("case", ["default", "env-wins", "missing", "unreadable",
                                 "absent", "private", "unmarked", "group", "other"])
def test_private_words_routing(repo, tmp_path, case):
    r, base = repo
    words = tmp_path / "home/.config/plangraph/scrub-words.txt"
    words.parent.mkdir(parents=True)
    words.write_text(fake_word() + "\n")
    words.chmod(0o600)
    env = {}
    if case == "env-wins":
        override = tmp_path / "override.txt"
        override.write_text("syntheticreplacement\n")
        env["PLANGRAPH_SCRUB_WORDS"] = str(override)
    if case in ("missing", "unreadable"):
        env["PLANGRAPH_SCRUB_WORDS"] = str(words)
        git(r, "config", "remote.origin.plangraph-private", "true")
        if case == "missing":
            words.unlink()
        else:
            words.chmod(0)
    if case == "absent":
        words.unlink()
    if case == "private":
        git(r, "config", "remote.origin.plangraph-private", "true")
    if case == "unmarked":
        git(r, "config", "remote.origin.url", "https://example.invalid/unreachable.git")
    if case in ("group", "other"):
        words.chmod(0o640 if case == "group" else 0o604)
    head = commit_file(r, "app/plan.py", fake_word() + "\n")
    res = run_hook(r, [push_line("main", head, base)], env)
    out = res.stdout + res.stderr
    assert res.returncode == (0 if case in ("env-wins", "absent", "private") else 1), out
    assert fake_word() not in out and str(words) not in out
    if case == "env-wins":
        assert str(override) not in out
        head = commit_file(r, "app/plan.py", "syntheticreplacement\n")
        res = run_hook(r, [push_line("main", head, base)], env)
        assert res.returncode == 1
        assert "syntheticreplacement" not in res.stdout + res.stderr
    if case in ("group", "other"):
        assert "chmod 600" in res.stderr
    if case == "absent":
        assert len([line for line in res.stderr.splitlines()
                    if "private-word rule is inactive" in line]) == 1
    if case in ("absent", "private"):
        head = commit_file(r, "app/net.py", fake_ip() + "\n")
        res = run_hook(r, [push_line("main", head, base)], env)
        assert res.returncode == 1
        assert "ipv4" in res.stdout + res.stderr
        assert fake_word() not in res.stdout + res.stderr
        assert fake_ip() not in res.stdout + res.stderr


@pytest.mark.parametrize("source", ["global", "system", "worktree", "environment"])
@pytest.mark.parametrize("explicit", [False, True])
def test_nonlocal_private_marker_cannot_skip_private_words(repo, tmp_path, source, explicit):
    r, base = repo
    words = tmp_path / "home/.config/plangraph/scrub-words.txt"
    words.parent.mkdir(parents=True)
    words.write_text(fake_word() + "\n")
    words.chmod(0o600)
    key = "remote.origin.plangraph-private"
    assert key not in git(r, "config", "--local", "--list")
    marker = tmp_path / "nonlocal.gitconfig"
    marker.write_text('[remote "origin"]\n\tplangraph-private = true\n')
    env = {}
    if source == "global":
        env["GIT_CONFIG_GLOBAL"] = str(marker)
    elif source == "system":
        env.update(GIT_CONFIG_SYSTEM=str(marker), GIT_CONFIG_NOSYSTEM="0")
    elif source == "worktree":
        git(r, "config", "extensions.worktreeConfig", "true")
        git(r, "config", "--worktree", key, "true")
    else:
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=key, GIT_CONFIG_VALUE_0="true")
    if explicit:
        env["PLANGRAPH_SCRUB_WORDS"] = str(words)
    head = commit_file(r, "app/plan.py", fake_word() + "\n")
    res = run_hook(r, [push_line("main", head, base)], env)
    out = res.stdout + res.stderr
    assert res.returncode == 1, out
    assert "app/plan.py:1: private-word" in out
    assert fake_word() not in out and str(words) not in out


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


def test_prepush_runs_changelog_docs_warn_only(repo):
    r, base = repo
    head = commit_file(r, "app/feature.py", "x = 42\n")
    res = run_hook(r, [push_line("main", head, base)])
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "check-changelog-docs: ok" in out


def test_prepush_checks_the_pushed_sha_not_head(repo):
    r, base = repo
    git(r, "checkout", "-q", "-b", "other")
    pushed = commit_file(r, "app/feature.py", "x = 42\n")
    git(r, "checkout", "-q", "main")
    commit_file(r, "docs/n.md", "# n\n")
    commit_file(r, "docs/m.md", "# m\n")
    res = run_hook(r, [push_line("other", pushed, base)])
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert "1 path(s)" in out and "changelog no" in out


@pytest.mark.parametrize('role', ['author', 'committer'])
def test_identity_gate_refuses_guessed_email(repo, monkeypatch, capsys, role):
    import runpy
    r, base = repo
    valid = FIXTURE_EMAIL
    guessed = 'writer@' + 'fixture.invalid'
    monkeypatch.chdir(r)
    for prefix in ('GIT_AUTHOR', 'GIT_COMMITTER'):
        monkeypatch.setenv(prefix + '_NAME', 'Fixture')
        monkeypatch.setenv(prefix + '_EMAIL', guessed if prefix.lower().endswith(role) else valid)
    git(r, 'commit', '--allow-empty', '-qm', 'identity fixture')
    head = git(r, 'rev-parse', 'HEAD').strip()
    hook = runpy.run_path(str(HOOK))
    assert hook['identity_gate'](base, head) == 1
    assert role in capsys.readouterr().err


def test_identity_gate_accepts_noreply_email(repo, monkeypatch):
    import runpy
    r, base = repo
    monkeypatch.chdir(r)
    for prefix in ('GIT_AUTHOR', 'GIT_COMMITTER'):
        monkeypatch.setenv(prefix + '_NAME', 'Fixture')
        monkeypatch.setenv(prefix + '_EMAIL', FIXTURE_EMAIL)
    git(r, 'commit', '--allow-empty', '-qm', 'identity fixture')
    assert runpy.run_path(str(HOOK))['identity_gate'](base, git(r, 'rev-parse', 'HEAD').strip()) == 0



def _identity_commit(repo, monkeypatch, *, author=FIXTURE_EMAIL, committer=FIXTURE_EMAIL):
    with monkeypatch.context() as patch:
        for prefix, email in [('GIT_AUTHOR', author), ('GIT_COMMITTER', committer)]:
            patch.setenv(prefix + '_NAME', 'Fixture')
            patch.setenv(prefix + '_EMAIL', email)
        git(repo, 'commit', '--allow-empty', '-qm', 'identity fixture')
    return git(repo, 'rev-parse', 'HEAD').strip()


def _merged_identity_history(repo, monkeypatch, remote='origin', bad_role=None):
    r, base = repo
    git(r, 'checkout', '-qb', 'lane')
    bad = 'writer@' + 'fixture.invalid'
    branch = _identity_commit(r, monkeypatch, **({bad_role: bad} if bad_role else {}))
    git(r, 'checkout', '-q', 'main')
    git(r, 'checkout', '-qb', 'upstream')
    _identity_commit(r, monkeypatch)
    git(r, 'checkout', '-q', 'main')
    with monkeypatch.context() as patch:
        patch.setenv('GIT_AUTHOR_NAME', 'Fixture')
        patch.setenv('GIT_AUTHOR_EMAIL', FIXTURE_EMAIL)
        patch.setenv('GIT_COMMITTER_NAME', 'GitHub')
        patch.setenv('GIT_COMMITTER_EMAIL', '@'.join(('noreply', 'github.com')))
        git(r, 'merge', '--no-ff', 'upstream', '-m', 'upstream merge')
    upstream = git(r, 'rev-parse', 'HEAD').strip()
    git(r, 'update-ref', f'refs/remotes/{remote}/main', upstream)
    git(r, 'checkout', '-q', 'lane')
    git(r, 'merge', '--no-ff', 'main', '-m', 'merge published main')
    return r, base, branch, upstream, git(r, 'rev-parse', 'HEAD').strip()


@pytest.mark.parametrize('remote', ['origin', 'publish'])
def test_identity_gate_accepts_merge_of_published_main(repo, monkeypatch, remote):
    r, base, _, upstream, head = _merged_identity_history(repo, monkeypatch, remote)
    assert len(git(r, 'show', '-s', '--format=%P', upstream).split()) == 2
    result = run_hook(r, [push_line('lane', head, base)], remote=remote)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('role', ['author', 'committer'])
def test_identity_gate_refuses_new_bad_identity_behind_main_merge(repo, monkeypatch, role):
    r, base, branch, _, head = _merged_identity_history(repo, monkeypatch, bad_role=role)
    result = run_hook(r, [push_line('lane', head, base)])
    assert result.returncode == 1
    assert f'{branch[:12]} {role} email outside noreply allowlist' in result.stderr


def test_identity_gate_missing_remote_main_keeps_strict_span(repo, monkeypatch):
    r, base, _, upstream, head = _merged_identity_history(repo, monkeypatch)
    git(r, 'update-ref', 'refs/remotes/origin/phase/published', upstream)
    git(r, 'update-ref', '-d', 'refs/remotes/origin/main')
    result = run_hook(r, [push_line('lane', head, base)])
    assert result.returncode == 1
    assert f'{upstream[:12]} committer email outside noreply allowlist' in result.stderr


@pytest.mark.parametrize('tracking_remote', ['origin', 'other'])
def test_identity_gate_only_excludes_pushed_remotes_published_phase(repo, monkeypatch, tracking_remote):
    r, base = repo
    git(r, 'update-ref', 'refs/remotes/origin/main', base)
    git(r, 'checkout', '-qb', 'published')
    published = _identity_commit(r, monkeypatch, committer='writer@' + 'fixture.invalid')
    git(r, 'update-ref', f'refs/remotes/{tracking_remote}/phase/published', published)
    git(r, 'checkout', '-qb', 'lane', base)
    _identity_commit(r, monkeypatch)
    git(r, 'merge', '--no-ff', 'published', '-m', 'merge phase')
    head = git(r, 'rev-parse', 'HEAD').strip()
    result = run_hook(r, [push_line('lane', head, base)])
    assert result.returncode == (0 if tracking_remote == 'origin' else 1), result.stderr
    if tracking_remote == 'other':
        assert f'{published[:12]} committer email outside noreply allowlist' in result.stderr


def test_identity_gate_still_checks_new_merge_commit(repo, monkeypatch):
    r, base, _, _, _ = _merged_identity_history(repo, monkeypatch)
    git(r, 'checkout', '-qb', 'fresh-side')
    _identity_commit(r, monkeypatch)
    git(r, 'checkout', '-q', 'lane')
    with monkeypatch.context() as patch:
        patch.setenv('GIT_COMMITTER_NAME', 'GitHub')
        patch.setenv('GIT_COMMITTER_EMAIL', '@'.join(('noreply', 'github.com')))
        git(r, 'merge', '--no-ff', 'fresh-side', '-m', 'new merge')
    head = git(r, 'rev-parse', 'HEAD').strip()
    result = run_hook(r, [push_line('lane', head, base)])
    assert result.returncode == 1
    assert f'{head[:12]} committer email outside noreply allowlist' in result.stderr

def test_pushed_tree_cap_overrides_older_hook_checkout(repo, tmp_path):
    import shutil
    r, base = repo
    head = commit_file(r, '.file-policy.json', '{"max_lines":800}\n')
    head = commit_file(r, 'sample.rs', 'x\n' * 600)
    old = tmp_path / 'older-hooks' / 'tools'
    (old / 'hooks').mkdir(parents=True)
    shutil.copy2(HOOK, old / 'hooks/pre-push')
    shutil.copytree(ROOT / 'tools/split', old / 'split')
    shutil.copy2(ROOT / 'tools/affected_tests.py', old / 'affected_tests.py')
    (old / 'check_file_size.py').write_text((ROOT / 'tools/check_file_size.py').read_text().replace('LIMIT = 800', 'LIMIT = 400'))
    result = subprocess.run([sys.executable, str(old / 'hooks/pre-push')], cwd=r,
        input=push_line('phase/fixture', head, base) + '\n', capture_output=True, text=True,
        env={**os.environ, 'PREFLIGHT_SKIP': '1', 'HOME': str(tmp_path / 'home')})
    assert result.returncode == 0, result.stdout + result.stderr


def test_phase_skip_cannot_bypass_affected_gate(repo):
    r, base = repo
    head = commit_file(r, 'tools/affected_tests.py',
        "from pathlib import Path\nPath('.affected-ran').write_text('yes')\n")
    result = run_hook(r, [push_line('phase/fixture', head, base)], {'PREFLIGHT_SKIP': '1'})
    assert result.returncode == 0, result.stderr
    assert (r / '.affected-ran').is_file()


def test_affected_gate_is_file_only_and_time_bounded(tmp_path, monkeypatch):
    import runpy
    from types import SimpleNamespace
    tools = tmp_path / 'tools'
    tools.mkdir()
    (tools / 'affected_tests.py').touch()
    monkeypatch.setenv('PREFLIGHT_SKIP', '1')
    monkeypatch.setenv('GIT_DIR', str(tmp_path / 'caller.git'))
    calls = []
    def popen(command, **kw):
        calls.append((command, kw))
        def wait(timeout):
            kw['timeout'] = timeout
            return 0
        return SimpleNamespace(wait=wait)
    monkeypatch.setattr(subprocess, 'Popen', popen)
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: 'GIT_DIR\n')
    hook = runpy.run_path(str(HOOK))
    assert hook['affected_gate'](tmp_path, 'base', 'head') == 0
    command, options = calls[0]
    assert '--files-only' in command and '--run' in command
    assert command[command.index('--max-files') + 1] == '40'
    assert Path(command[1]).name == 'affected_tests.py'
    assert options['timeout'] <= 240
    forwarded_git_dir = options['env'].get('GIT_DIR')
    assert forwarded_git_dir is None
    assert 'preflight.py' not in ' '.join(command)


@pytest.mark.skipif(sys.platform != 'linux', reason='real process-group check uses proc state')
def test_selector_timeout_kills_its_worker_group(tmp_path):
    import runpy
    import signal
    import time
    selector = tmp_path / 'tools/affected_tests.py'
    selector.parent.mkdir()
    marker = tmp_path / 'worker.pid'
    selector.write_text('import subprocess, sys, time\nfrom pathlib import Path\n'
        "worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', 'd2-child-fixture'])\n"
        f'Path({str(marker)!r}).write_text(str(worker.pid))\ntime.sleep(30)\n')
    hook = runpy.run_path(str(HOOK))
    def alive(pid):
        path = Path('/proc') / str(pid) / 'stat'
        try:
            return path.read_text().rsplit(')', 1)[1].split()[0] != 'Z'
        except (FileNotFoundError, ProcessLookupError):
            return False
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            hook['affected_gate'](tmp_path, 'base', 'head', timeout=.8)
        assert marker.is_file(), 'fixture worker did not start before the timeout'
        pid = int(marker.read_text())
        until = time.monotonic() + 1
        while alive(pid) and time.monotonic() < until:
            time.sleep(.01)
        assert not alive(pid), 'selector worker survived hook refusal'
    finally:
        if marker.exists():
            pid = int(marker.read_text())
            command = Path('/proc') / str(pid) / 'cmdline'
            try:
                owned = b'd2-child-fixture' in command.read_bytes()
            except (FileNotFoundError, ProcessLookupError):
                owned = False
            if owned:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.mark.parametrize('platform', ['posix', 'nt'])
@pytest.mark.parametrize('interrupted', [False, True])
def test_selector_timeout_uses_group_cleanup_on_each_platform(tmp_path, monkeypatch, platform, interrupted):
    import runpy
    from fake_pid import FAKE_PID
    from types import SimpleNamespace
    hook = runpy.run_path(str(HOOK))
    (tmp_path / 'tools').mkdir()
    (tmp_path / 'tools/affected_tests.py').touch()
    created, killed, commands = [], [], []
    class Process:
        pid = FAKE_PID
        waits = 0
        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                if interrupted: raise KeyboardInterrupt
                raise subprocess.TimeoutExpired('selector', timeout)
            return -9
        def kill(self):
            killed.append('parent')
    monkeypatch.setattr(subprocess, 'Popen', lambda command, **kw: created.append(kw) or Process())
    monkeypatch.setattr(subprocess, 'run', lambda command, **kw: commands.append(command) or SimpleNamespace(returncode=0))
    globals_ = hook['affected_gate'].__globals__
    monkeypatch.setitem(globals_, 'test_environment', lambda *a: {})
    monkeypatch.setitem(globals_, 'os', SimpleNamespace(name=platform, environ={}, killpg=lambda *a: killed.append(a)))
    monkeypatch.setitem(globals_, 'signal', SimpleNamespace(SIGKILL=9))
    with pytest.raises(KeyboardInterrupt if interrupted else subprocess.TimeoutExpired):
        hook['affected_gate'](tmp_path, 'base', 'head', timeout=.1)
    if platform == 'nt':
        assert created[0]['creationflags'] == getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200)
        assert commands == [['taskkill', '/F', '/T', '/PID', str(FAKE_PID)]]
    else:
        assert created[0]['start_new_session'] is True
        assert killed[0] == (FAKE_PID, 9)
    assert killed[-1] == 'parent'


def test_capped_hook_timeout_still_refuses(tmp_path, monkeypatch, capsys):
    import runpy
    from types import SimpleNamespace
    from fake_pid import FAKE_PID
    selector = tmp_path / 'tools/affected_tests.py'
    selector.parent.mkdir()
    selector.touch()
    hook = runpy.run_path(str(HOOK))
    commands = []
    class Process:
        pid = FAKE_PID
        waits = 0
        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired('selector', timeout)
            return -9
        def kill(self):
            pass
    monkeypatch.setattr(subprocess, 'Popen', lambda command, **kw: commands.append(command) or Process())
    globals_ = hook['affected_gate'].__globals__
    monkeypatch.setitem(globals_, 'test_environment', lambda *a: {})
    monkeypatch.setitem(globals_, 'os', SimpleNamespace(name='posix', environ={}, killpg=lambda *a: None))
    monkeypatch.setitem(globals_, 'push_main', lambda: hook['affected_gate'](tmp_path, 'base', 'head', timeout=.01))
    assert hook['main']() == 1
    assert capsys.readouterr().err == 'pre-push: subprocess timeout: push refused\n'
    assert commands[0][commands[0].index('--max-files') + 1] == '40'
