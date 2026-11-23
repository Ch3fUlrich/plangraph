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
        assert ("origin/main unavailable" if case == "unknown-main" else "private-word") in out


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
        git(r, "push", str(remote), f"{base}:refs/heads/topic")
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
    monkeypatch.delenv("PREFLIGHT_SKIP", raising=False)
    try:
        git(r, "push", str(remote), f"{source}:refs/heads/topic")
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"installed pre-push hook refused the push:\n{exc.stderr}")
    assert marker.exists(), "Git must invoke the installed pre-push hook"
    assert git(remote, "rev-parse", "refs/heads/topic").strip() == head


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
    git(r, "update-ref", "refs/remotes/origin/main", head)
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
    base = hook['affected_base_commit'](ZERO, head)
    assert base == (phase if stacked in ('behind', 'ahead') else main)
    paths = git(r, 'diff', '--name-only', base, head).splitlines()
    assert paths == (['feature.py'] if stacked in ('behind', 'ahead') else ['feature.py', 'phase.py'])


@pytest.mark.parametrize('known_main', [False, True])
def test_new_stacked_branch_scrubs_inherited_phase_additions(repo, tmp_path, known_main):
    r, main = repo
    if known_main:
        git(r, 'update-ref', 'refs/remotes/origin/main', main)
    phase = commit_file(r, 'phase.txt', fake_word() + '\n')
    git(r, 'update-ref', 'refs/remotes/origin/phase/line-green-1', phase)
    head = commit_file(r, 'feature.txt', 'clean feature\n')
    words = tmp_path / 'words.txt'
    words.write_text(fake_word() + '\n')
    result = run_hook(r, [push_line('stacked', head, ZERO)], {'PLANGRAPH_SCRUB_WORDS': str(words)})
    assert result.returncode == 1, result.stderr
    assert ':1: private-word' in result.stdout + result.stderr
    assert fake_word() not in result.stdout + result.stderr


@pytest.mark.parametrize('case', ['refresh', 'main-ahead', 'divergent-remote',
                                  'unknown-main', 'unrelated-merge', 'no-merge'])
def test_pushed_branch_refresh_affected_base(repo, monkeypatch, case):
    import runpy
    r, initial = repo
    remote = commit_file(r, 'published.txt', 'published branch\n')
    git(r, 'checkout', '-q', '-b', 'lane')
    commit_file(r, 'branch.txt', 'branch work\n')
    git(r, 'checkout', '-q', 'main')
    if case == 'divergent-remote':
        git(r, 'reset', '--hard', initial)
    commit_file(r, 'upstream-one.txt', 'unrelated main work\n')
    main = commit_file(r, 'upstream-two.txt', 'more unrelated main work\n')
    git(r, 'update-ref', 'refs/remotes/origin/main', main)
    git(r, 'checkout', '-q', 'lane')
    if case == 'unrelated-merge':
        git(r, 'checkout', '-q', '-b', 'other', remote)
        commit_file(r, 'other.txt', 'other branch work\n')
        git(r, 'checkout', '-q', 'lane')
        git(r, 'merge', '--no-ff', 'other', '-m', 'merge other branch')
    elif case != 'no-merge':
        git(r, 'merge', '--no-ff', 'main', '-m', 'refresh from main')
    head = commit_file(r, 'after.txt', 'change after refresh\n')
    if case == 'main-ahead':
        git(r, 'checkout', '-q', 'main')
        newer = commit_file(r, 'upstream-later.txt', 'not merged yet\n')
        git(r, 'update-ref', 'refs/remotes/origin/main', newer)
    elif case == 'unknown-main':
        git(r, 'update-ref', '-d', 'refs/remotes/origin/main')
    monkeypatch.chdir(r)
    hook = runpy.run_path(str(HOOK))
    affected = hook['affected_base_commit'](remote, head)
    narrowed = case in ('refresh', 'main-ahead')
    assert affected == (main if narrowed else remote)
    paths = git(r, 'diff', '--name-only', affected, head).splitlines()
    if narrowed:
        assert paths == ['after.txt', 'branch.txt']
    elif case in ('divergent-remote', 'unknown-main'):
        assert 'upstream-one.txt' in paths and 'upstream-two.txt' in paths
    assert hook['base_commit'](remote, head) == remote
    selector = runpy.run_path(str(HOOK.parents[1] / 'affected_tests.py'))
    batch = selector['pushed_parts_files'](r, remote, head)
    if narrowed:
        assert batch == (paths, 1)
    else:
        assert batch is None


@pytest.mark.parametrize('case', ['resolution', 'unpushed', 'rewritten', 'normal',
                                  'shared', 'evil', 'clean', 'deleted-remote', 'fetch-failed'])
@pytest.mark.parametrize('new_branch', [False, True])
def test_pushed_parts_selection(repo, tmp_path, monkeypatch, case, new_branch):
    """Real published heads and production selector, including unsafe fallbacks."""
    import json
    import os
    from pathlib import Path
    import runpy
    import sys
    r, _ = repo
    for name in ('left', 'right', 'shared', 'normal', 'evil'):
        commit_file(r, f'tests/test_{name}.py', 'VALUE = 0\n')
    base = git(r, 'rev-parse', 'HEAD').strip()
    origin = tmp_path / 'origin.git'
    git(r, 'init', '--bare', str(origin))
    git(r, 'remote', 'add', 'origin', str(origin))
    git(r, 'push', '-q', 'origin', 'main')
    git(r, 'checkout', '-qb', 'left', base)
    commit_file(r, 'tests/test_left.py', 'VALUE = 1\n')
    if case in ('resolution', 'shared'):
        commit_file(r, 'tests/test_shared.py', 'VALUE = 1\n')
    git(r, 'push', '-q', 'origin', 'left')
    git(r, 'checkout', '-qb', 'right', base)
    commit_file(r, 'tests/test_right.py', 'VALUE = 1\n')
    if case in ('resolution', 'shared'):
        commit_file(r, 'tests/test_shared.py',
                    'VALUE = 2\n' if case == 'resolution' else 'VALUE = 1\n')
    if case != 'unpushed':
        git(r, 'push', '-q', 'origin', 'right')
    if case == 'rewritten':
        commit_file(r, 'tests/test_right.py', 'VALUE = 3\n')
        git(r, 'reset', '--soft', base)
        git(r, 'commit', '-qm', 'rewritten right')
    if case == 'deleted-remote':
        git(r, 'push', '-q', 'origin', ':right')
    git(r, 'checkout', '-qb', 'batch', base)
    git(r, 'merge', '--no-ff', 'left', '-m', 'merge left')
    if case == 'resolution':
        result = subprocess.run(['git', 'merge', '--no-ff', 'right', '-m', 'merge right'],
                                cwd=r, capture_output=True)
        assert result.returncode == 1
        commit_file(r, 'tests/test_shared.py', 'VALUE = 42\n')
    elif case == 'evil':
        git(r, 'merge', '--no-ff', '--no-commit', 'right')
        commit_file(r, 'tests/test_evil.py', 'VALUE = 42\n')
    else:
        git(r, 'merge', '--no-ff', 'right', '-m', 'merge right')
    if case == 'normal':
        commit_file(r, 'tests/test_normal.py', 'VALUE = 1\n')
    head = git(r, 'rev-parse', 'HEAD').strip()
    monkeypatch.chdir(r)
    hook = runpy.run_path(str(HOOK))
    # The hook refreshes origin before supplying its exact range to preflight.
    if case == 'fetch-failed':
        git(r, 'remote', 'set-url', 'origin', str(tmp_path / 'missing.git'))
    fetched = hook['refresh_origin']()
    assert fetched == (case != 'fetch-failed')
    remote_sha = ZERO if new_branch else base
    affected_base = hook['affected_base_commit'](remote_sha, head)
    batch_base = (hook['base_commit'](remote_sha, head) if new_branch else
                  hook['pushed_base_commit'](remote_sha, head))
    assert batch_base == base
    env = dict(os.environ, PLANGRAPH_PUSH_RANGE=f'{batch_base}:{head}')
    if not fetched:
        env['PLANGRAPH_PUSH_RANGE'] = ''
    result = subprocess.run(
        [sys.executable, str(Path(HOOK).parents[1] / 'affected_tests.py'),
         '--root', str(r), '--base', affected_base, '--head', head, '--files-only', '--local'],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=True)
    # Production preflight merges the streams and parses JSON on line zero for
    # timeout diagnostics; the mode log must follow that machine-readable plan.
    plan = json.loads(result.stdout.splitlines()[0])
    fallback = case in ('unpushed', 'rewritten', 'deleted-remote', 'fetch-failed')
    expected = (['tests/test_left.py', 'tests/test_right.py'] if fallback else
                [f"tests/test_{ {'resolution': 'shared', 'shared': 'shared'}.get(case, case)}.py"])
    if fallback and new_branch:
        expected = ['tests/test_right.py']  # Today's nearest-remote-base selection.
    if case == 'clean':
        expected = []
    assert plan['tests'] == expected
    assert f"mode={'normal' if fallback else 'pushed-parts'}" in result.stdout
    assert f'parts={0 if fallback else 2}' in result.stdout
    assert f'selected_tests={len(expected)}' in result.stdout


def test_incremental_batch_preserves_published_part_overlap(repo, tmp_path, monkeypatch):
    import json
    import os
    import runpy
    import sys
    r, _ = repo
    base = commit_file(r, 'tests/test_shared.py', 'LEFT = 0\n\n\nRIGHT = 0\n')
    origin = tmp_path / 'origin.git'
    git(r, 'init', '--bare', str(origin))
    git(r, 'remote', 'add', 'origin', str(origin))
    git(r, 'push', '-q', 'origin', 'main')
    git(r, 'checkout', '-qb', 'left', base)
    commit_file(r, 'tests/test_shared.py', 'LEFT = 1\n\n\nRIGHT = 0\n')
    git(r, 'push', '-q', 'origin', 'left')
    git(r, 'checkout', '-qb', 'right', base)
    commit_file(r, 'tests/test_shared.py', 'LEFT = 0\n\n\nRIGHT = 1\n')
    git(r, 'push', '-q', 'origin', 'right')
    git(r, 'checkout', '-qb', 'batch', base)
    git(r, 'merge', '--no-ff', 'left', '-m', 'merge left')
    remote = git(r, 'rev-parse', 'HEAD').strip()
    git(r, 'push', '-q', 'origin', 'batch')
    git(r, 'merge', '--no-ff', 'right', '-m', 'merge right')
    head = git(r, 'rev-parse', 'HEAD').strip()
    monkeypatch.chdir(r)
    hook = runpy.run_path(str(HOOK))
    assert hook['refresh_origin']()
    result = subprocess.run(
        [sys.executable, str(HOOK.parents[1] / 'affected_tests.py'), '--root', str(r),
         '--base', hook['affected_base_commit'](remote, head), '--head', head,
         '--files-only', '--local'], env=dict(os.environ, PLANGRAPH_PUSH_RANGE=f'{remote}:{head}'),
        capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)['tests'] == ['tests/test_shared.py']
    assert 'mode=normal' in result.stderr


def _install_marker_preflight(r, marker):
    (r / "tools").mkdir(parents=True, exist_ok=True)
    (r / "tools/preflight.py").write_text(
        "import os, sys\nfrom pathlib import Path\n"
        "assert '--fast' not in sys.argv\n"
        "with Path(os.environ['TEST_PREFLIGHT_MARKER']).open('a') as marker:\n"
        "    marker.write('ran\\n')\n"
    )
    return {"TEST_PREFLIGHT_MARKER": str(marker)}


@pytest.mark.parametrize("ref", ["phase/x", "phase/nested/y", "feat/x", "lane/x", "main", "phase-x"])
def test_preflight_skip_scope_allows_only_phase_refs(repo, tmp_path, ref):
    """Legacy phase skip flags cannot suppress affected preflight on any ref."""
    r, base = repo
    marker = tmp_path / "preflight-ran"
    env = _install_marker_preflight(r, marker)
    head = commit_file(r, "tools/preflight.py", (r / "tools/preflight.py").read_text())
    res = run_hook(r, [push_line(ref, head, base)],
                   {**env, "PREFLIGHT_SKIP": "1", "PREFLIGHT_FAST": "1"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert marker.read_text() == "ran\n"


def test_preflight_skip_multi_ref_and_other_gates(repo, tmp_path):
    """Legacy phase skip flags leave every ref and the scrub gate covered."""
    r, base = repo
    marker = tmp_path / "preflight-ran"
    env = {**_install_marker_preflight(r, marker), "PREFLIGHT_SKIP": "1"}
    clean_head = commit_file(r, "tools/preflight.py", (r / "tools/preflight.py").read_text())
    mixed = run_hook(r, [push_line("phase/x", clean_head, base),
                         push_line("feat/x", clean_head, base),
                         f"HEAD {clean_head} refs/tags/v1 {ZERO}"], env)
    assert mixed.returncode == 0, mixed.stdout + mixed.stderr
    assert marker.read_text().splitlines() == ["ran"] * 3
    marker.unlink()
    dirty_head = commit_file(r, "app/leak.py", f'peer = "{fake_ip()}"\n')
    dirty = run_hook(
        r,
        [push_line("phase/x", dirty_head, clean_head)],
        {**env, "PREFLIGHT_SKIP": "1"},
    )
    assert dirty.returncode == 1, dirty.stdout + dirty.stderr
    assert "app/leak.py:1: ipv4" in dirty.stdout + dirty.stderr
    assert not marker.exists()


def test_phase_hook_runs_only_affected_tests_with_a_fixed_deadline(repo):
    import site
    import sys
    import time

    r, _ = repo
    preflight = HOOK.parents[1] / "preflight.py"
    user_base = site.getuserbase()
    # Keep the hook, preflight argument construction, timeout and selector real.
    # The unrelated scrub, policy and Rust subprocesses are covered separately.
    launcher = f"""
from pathlib import Path
import os
import runpy
import site

assert "PREFLIGHT_TOOLS_DIR" not in os.environ
assert site.getuserbase() == {user_base!r}
main = runpy.run_path({str(preflight)!r})["main"]
settings = main.__globals__
run = settings["run"]
run_test_process = settings["run_test_process"]

def affected_only(command, cwd, env):
    if settings["is_affected_step"](command):
        return run(command, cwd, env)
    return 0, ""

def run_with_deadline(command, **kwargs):
    assert {{"--files-only", "--local", "--run"}} <= set(command)
    assert kwargs["timeout"] == 240
    return run_test_process(command, **kwargs)

def unexpected_plan(*args):
    raise AssertionError("File-only runs must share one deadline with selection")

assert "planned_tests" in settings
settings.update(run=affected_only, run_test_process=run_with_deadline,
                planned_tests=unexpected_plan, trim_shared_target=lambda root: None)
raise SystemExit(main(root=Path.cwd()))
"""
    commit_file(r, "tools/preflight.py", launcher)
    base = commit_file(r, "tests/test_unaffected.py",
                       'raise AssertionError("the full suite was collected")\n')
    git(r, "update-ref", "refs/remotes/origin/main", base)
    head = commit_file(r, "tests/test_selected.py", "def test_selected():\n    assert True\n")
    env = {"PREFLIGHT_PYTHON": sys.executable, "PREFLIGHT_TEST_WORKERS": "1",
           "PREFLIGHT_SKIP": "1", "PREFLIGHT_FAST": "1",
           "PREFLIGHT_TOOLS_DIR": str(r / "ambient-tools")}
    if user_base is not None:
        env["PYTHONUSERBASE"] = user_base
    started = time.monotonic()
    result = run_hook(r, [push_line("phase/affected", head, base)], env)
    elapsed = time.monotonic() - started
    print(f"phase pre-push replay wall seconds: {elapsed:.3f}")
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert '"tests": ["tests/test_selected.py"]' in output
    assert "1 passed" in output
    assert elapsed < 300
