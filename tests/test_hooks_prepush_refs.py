"""Ref-safety coverage for tools/hooks/pre-push, including installed-hook pushes."""
import shlex
import subprocess

import pytest

from test_hooks_prepush import (
    HOOK, ZERO, commit_file, fake_ip, fake_word, git, push_line, repo, run_hook,
)


@pytest.mark.parametrize("case", ["main-only", "branch-hit", "resolution-hit",
                                 "touched-clean", "unknown-main", "base-only"])
def test_merge_main_scrubs_only_branch_additions(repo, tmp_path, case):
    r, base = repo
    words = tmp_path / "words.txt"
    words.write_text(fake_word() + "\n")
    git(r, "checkout", "-q", "-b", "lane")
    remote = commit_file(r, "app/lane.txt", "clean lane\n")
    git(r, "checkout", "-q", "main")
    main = commit_file(r, "app/shared.txt", fake_word() + "\n")
    git(r, "update-ref", "refs/remotes/origin/main", main)
    git(r, "checkout", "-q", "lane")
    if case == "resolution-hit":
        commit_file(r, "app/shared.txt", "clean branch\n")
        merge = subprocess.run(
            ["git", "merge", "--no-ff", "main", "-m", "merge main"],
            cwd=r, capture_output=True, text=True)
        assert merge.returncode == 1, "expected an add/add conflict"
        # Neither parent contains this added line; only the resolution does.
        commit_file(r, "app/shared.txt", "clean resolution\n")
        git(r, "checkout", "-q", "main")
        main = commit_file(r, "app/shared.txt", "clean upstream\n")
        git(r, "update-ref", "refs/remotes/origin/main", main)
        git(r, "checkout", "-q", "lane")
        # Recreate a conflict whose parents are both clean.
        merge = subprocess.run(
            ["git", "merge", "--no-ff", "main", "-m", "merge again"],
            cwd=r, capture_output=True, text=True)
        assert merge.returncode == 1, "expected a content conflict"
        head = commit_file(r, "app/shared.txt", fake_word() + " resolution\n")
    else:
        git(r, "merge", "--no-ff", "main", "-m", "merge main")
        head = git(r, "rev-parse", "HEAD").strip()
        if case == "branch-hit":
            head = commit_file(r, "app/added.txt", fake_word() + "\n")
        elif case in ("touched-clean", "unknown-main"):
            head = commit_file(r, "app/shared.txt", fake_word() + "\nclean addition\n")
        elif case == "base-only":
            git(r, "checkout", "-q", "main")
            main = commit_file(r, "app/shared.txt", "clean upstream\n")
            git(r, "update-ref", "refs/remotes/origin/main", main)
        if case == "unknown-main":
            git(r, "update-ref", "-d", "refs/remotes/origin/main")
    res = run_hook(r, [push_line("lane", head, remote)],
                   {"PLANGRAPH_SCRUB_WORDS": str(words)})
    out = res.stdout + res.stderr
    assert fake_word() not in out, "private fixture leaked"
    expected = 1 if case in ("branch-hit", "resolution-hit", "unknown-main") else 0
    assert res.returncode == expected, out
    if expected:
        assert "private-word" in out


@pytest.mark.parametrize("remote_ref", ["refs/heads/main", "refs/tags/release",
                                        "refs/custom/review"])
@pytest.mark.parametrize("case", ["non-ff", "delete", "missing"])
@pytest.mark.parametrize("unsafe_first", [False, True])
def test_hook_refuses_unsafe_ref_updates(repo, tmp_path, remote_ref, case, unsafe_first):
    r, base = repo
    remote = commit_file(r, "app/other.py", "y = 2\n")
    local = base
    if case == "delete":
        local = ZERO
    elif case == "missing":
        remote = "1" * 40
    lines = [push_line("safe", base, ZERO),
             f"refs/heads/main {local} {remote_ref} {remote}"]
    if unsafe_first:
        lines.reverse()
    res = run_hook(r, lines, {"PLANGRAPH_SCRUB_WORDS": str(tmp_path / "missing")})
    out = res.stdout + res.stderr
    assert res.returncode == 1, out
    action = "delete" if case == "delete" else "non-fast-forward update"
    expected = (f"pre-push: remote object unavailable for {remote_ref}" if case == "missing"
                else f"pre-push: refused {action} of {remote_ref}")
    if case == "non-ff" and remote_ref.startswith("refs/tags/"):
        expected = f"refused update of existing tag {remote_ref}: tags are immutable"
    assert expected in out
    assert "scrub" not in out and "file-size" not in out


@pytest.mark.parametrize("new_branch", [False, True])
def test_hook_passes_safe_ref_updates(repo, new_branch):
    r, base = repo
    head = commit_file(r, "app/other.py", "y = 2\n")
    res = run_hook(r, [push_line("feature", head, ZERO if new_branch else base)])
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.parametrize("source", ["@", "HEAD~1"])
@pytest.mark.parametrize("new_branch", [False, True])
def test_push_revision_expression_uses_installed_hook(
        repo, tmp_path, monkeypatch, source, new_branch):
    r, base = repo
    remote = tmp_path / "remote.git"
    git(r, "init", "--bare", str(remote))
    if not new_branch:
        git(r, "push", str(remote), f"{base}:refs/heads/main")
    head = commit_file(r, "app/next.py", "next = 2\n")
    if source == "HEAD~1":
        commit_file(r, "app/later.py", "later = 3\n")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-invoked"
    wrapper = hooks / "pre-push"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"touch {shlex.quote(str(marker))}\n"
        f'exec {shlex.quote(str(HOOK))} "$@"\n'
    )
    wrapper.chmod(0o755)
    git(r, "config", "core.hooksPath", str(hooks))
    # Like run_hook(), isolate ref-safety coverage from preflight: this tiny
    # repository deliberately has no tools/preflight.py or project toolchain.
    monkeypatch.setenv("PREFLIGHT_SKIP", "1")
    try:
        git(r, "push", str(remote), f"{source}:refs/heads/main")
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"installed pre-push hook refused the push:\n{exc.stderr}")
    assert marker.exists(), "Git must invoke the installed pre-push hook"
    assert git(remote, "rev-parse", "refs/heads/main").strip() == head


def test_hook_refuses_lightweight_tag_fast_forward(repo):
    r, base = repo
    git(r, "tag", "release", base)
    head = commit_file(r, "app/next.py", "next = 2\n")
    assert git(r, "merge-base", "--is-ancestor", base, head) == ""
    res = run_hook(r, [f"HEAD {head} refs/tags/release {base}"])
    assert res.returncode == 1
    assert ("refused update of existing tag refs/tags/release: tags are immutable"
            in res.stderr)
    assert "non-fast-forward" not in res.stderr
    unchanged = run_hook(r, [f"release {base} refs/tags/release {base}"])
    assert unchanged.returncode == 0, unchanged.stdout + unchanged.stderr
    created = run_hook(r, [f"HEAD {head} refs/tags/new {ZERO}"])
    assert created.returncode == 0, created.stdout + created.stderr


@pytest.mark.parametrize("target", ["tag", "commit", "blob", "tree"])
@pytest.mark.parametrize("source", ["tag", "blob", "tree"])
def test_hook_refuses_noncommit_moves(repo, source, target):
    r, base = repo
    git(r, "tag", "-a", "release", "-m", "release")
    old_tag = git(r, "rev-parse", "release").strip()
    head = commit_file(r, "app/next.py", "next = 2\n")
    git(r, "tag", "-a", "next", "-m", "next")
    old = {"tag": old_tag, "blob": git(r, "rev-parse", f"{base}:app/clean.py").strip(),
           "tree": git(r, "rev-parse", f"{base}^{{tree}}").strip()}[source]
    new = {"tag": git(r, "rev-parse", "next").strip(), "commit": head,
           "blob": git(r, "rev-parse", f"{head}:app/next.py").strip(),
           "tree": git(r, "rev-parse", f"{head}^{{tree}}").strip()}[target]
    ref = "refs/custom/object"
    res = run_hook(r, [f"refs/heads/main {new} {ref} {old}"])
    assert res.returncode == 1
    assert f"refused non-fast-forward update of {ref}" in res.stderr


@pytest.mark.parametrize("line", ["", "one two three", "a b c d extra",
                                      "refs/heads/main xyz refs/heads/main xyz",
                                      "refs/heads/main {oid} bad..ref {oid}",
                                      "HEAD {oid} refs/heads/bad..ref {oid}"])
def test_hook_refuses_malformed_records(repo, line):
    r, base = repo
    res = run_hook(r, [line.format(oid=base)])
    assert res.returncode == 1
    assert "pre-push: malformed push record" in res.stderr


def test_hook_ignores_replace_objects(repo):
    r, base = repo
    remote = commit_file(r, "app/remote.py", "remote = 1\n")
    tree = git(r, "rev-parse", f"{base}^{{tree}}").strip()
    replacement = git(r, "commit-tree", tree, "-p", remote, "-m", "replacement").strip()
    git(r, "replace", base, replacement)
    assert git(r, "merge-base", "--is-ancestor", remote, base) == ""
    res = run_hook(r, [push_line("main", base, remote)])
    assert res.returncode == 1
    assert "refused non-fast-forward update" in res.stderr


@pytest.mark.parametrize("case, expected", [
    ("non-ff", "pre-push: refused non-fast-forward update of refs/heads/main"),
    ("delete", "pre-push: refused delete of refs/heads/main"),
])
def test_script_push_uses_installed_hook(repo, tmp_path, case, expected):
    r, base = repo
    remote = tmp_path / "remote.git"
    git(r, "init", "--bare", str(remote))
    head = commit_file(r, "app/remote.py", "remote = 1\n")
    git(r, "push", str(remote), f"{head}:refs/heads/main")
    git(r, "config", "core.hooksPath", str(HOOK.parent))
    script = tmp_path / "push.sh"
    script.write_text('#!/bin/bash\ngit push --force "$1" "$2:refs/heads/main"\n')
    source = base if case == "non-ff" else ""
    res = subprocess.run(["bash", str(script), str(remote), source], cwd=r,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode != 0
    assert expected in res.stderr
    assert git(remote, "rev-parse", "refs/heads/main").strip() == head


def test_sha256_new_branch_and_delete(tmp_path):
    r = tmp_path / "sha256"
    git(tmp_path, "init", "--object-format=sha256", str(r))
    git(r, "config", "user.name", "t")
    git(r, "config", "user.email", "t@example.invalid")
    head = commit_file(r, "clean.txt", "clean\n")
    zero = "0" * len(head)
    res = run_hook(r, [push_line("feature", head, zero)])
    assert res.returncode == 0, res.stderr
    assert "scrub hit(s)" not in res.stdout + res.stderr
    dirty = commit_file(r, "app/net.py", fake_ip() + "\n")
    res = run_hook(r, [push_line("feature", dirty, zero)])
    assert res.returncode == 1, res.stdout + res.stderr
    assert "app/net.py:1: ipv4" in res.stdout + res.stderr
    assert fake_ip() not in res.stdout + res.stderr
    res = run_hook(r, [push_line("feature", zero, head)])
    assert res.returncode == 1
    assert "refused delete" in res.stderr


@pytest.mark.parametrize('stacked', ['none', 'behind', 'ahead', 'unrelated'])
def test_new_branch_preflight_uses_nearest_remote_base(repo, monkeypatch, stacked):
    import runpy
    r, main = repo
    git(r, 'update-ref', 'refs/remotes/origin/main', main)
    phase = commit_file(r, 'phase.py', 'phase = 1\n')
    if stacked != 'none':
        remote = phase
        if stacked in ('ahead', 'unrelated'):
            tree = git(r, 'rev-parse', phase + '^{tree}').strip()
            remote = git(r, 'commit-tree', tree, '-p', phase if stacked == 'ahead' else main,
                         '-m', 'remote advancement').strip()
        git(r, 'update-ref', 'refs/remotes/origin/phase/line-green-1', remote)
    head = commit_file(r, 'feature.py', 'feature = 1\n')
    monkeypatch.chdir(r)
    hook = runpy.run_path(str(HOOK))
    base = hook['base_commit'](ZERO, head)
    assert base == (phase if stacked in ('behind', 'ahead') else main)
    paths = git(r, 'diff', '--name-only', base, head).splitlines()
    assert paths == (['feature.py'] if stacked in ('behind', 'ahead') else ['feature.py', 'phase.py'])
