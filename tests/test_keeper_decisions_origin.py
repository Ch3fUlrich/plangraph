"""Decision evidence follows a local bare origin, never the stale checkout."""
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from plangraph.keeper import evidence


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, timeout=30).stdout.strip()


@pytest.fixture
def data_repos(tmp_path, monkeypatch, request):
    # Ignore caller hooks/config and repository-local Git environment, including
    # the context inherited when these tests run inside the pre-push hook.
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    for name, value in {
        "HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file",
    }.items():
        monkeypatch.setenv(name, value)
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    clone = tmp_path / "data"
    git(tmp_path, "clone", str(origin), str(clone))
    relative = getattr(request, "param", "router/docs/decisions-log.md")
    log = clone / relative
    log.parent.mkdir(parents=True)
    initial = "| D-1 | demo | Initial decision |\n"
    log.write_text(initial, encoding="utf-8")
    git(clone, "add", ".")
    git(clone, "commit", "-m", "Initial decisions")
    git(clone, "push", "origin", "main")
    base = git(clone, "rev-parse", "HEAD")
    worktree = tmp_path / "writer"
    git(clone, "worktree", "add", "-b", "decisions-update", str(worktree))
    return SimpleNamespace(
        origin=origin, clone=clone, log=log, relative=relative,
        worktree=worktree, base=base, initial=initial,
        ctx=evidence.default_context({"decisions_file": str(log)}))


def verify(ctx, decision="D-5571"):
    return evidence.verify("decision", {"key": "demo:A"}, {"id": decision}, ctx)


def push_decision(repos):
    (repos.worktree / repos.relative).write_text(
        repos.initial + "| D-5571 | demo | Published by a worktree |\n",
        encoding="utf-8")
    git(repos.worktree, "commit", "-am", "Publish decision")
    git(repos.worktree, "push", "origin", "HEAD:main")
    return git(repos.worktree, "rev-parse", "HEAD")


@pytest.mark.parametrize("data_repos", [
    "router/docs/decisions-log.md", "notes/nested/custom decisions.md",
], indirect=True)
def test_pushed_worktree_decision_verifies_in_one_sync_with_stale_checkout(data_repos, monkeypatch):
    repos = data_repos
    assert not verify(repos.ctx).ok
    head = push_decision(repos)
    # A worktree push updates the clone's shared remote-tracking ref too. Rewind
    # only this throwaway cache to prove verification actually fetches the row.
    git(repos.clone, "update-ref", "refs/remotes/origin/main", repos.base)
    run, calls = subprocess.run, []

    def recorded(args, **kwargs):
        calls.append(args)
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recorded)
    result = verify(repos.ctx)
    assert result.ok, result.detail
    assert result.detail == "decision D-5571 is recorded"
    assert [args[3] for args in calls] == ["rev-parse", "fetch", "cat-file"]
    assert calls[-1][-2] == "blob"
    assert calls[-1][-1] == f"origin/main:{repos.relative}"
    assert git(repos.clone, "rev-parse", "origin/main") == head
    assert git(repos.clone, "rev-parse", "HEAD") == repos.base
    assert repos.log.read_text(encoding="utf-8") == repos.initial


@pytest.mark.parametrize("recorded", [True, False])
def test_origin_main_rewrite_refreshes_tracking_ref_in_one_sync(data_repos, monkeypatch, recorded):
    repos = data_repos
    head = push_decision(repos)
    tree = f"{head if recorded else repos.base}^{{tree}}"
    rewritten = git(repos.origin, "commit-tree", tree, "-m", "Rewrite decisions authority")
    git(repos.origin, "update-ref", "refs/heads/main", rewritten, head)
    run, fetches = subprocess.run, []

    def capture_fetch(args, **kwargs):
        if "fetch" in args:
            fetches.append(args)
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture_fetch)
    result = verify(repos.ctx)
    assert result.ok is recorded, result.detail
    assert result.detail == f"decision D-5571 is {'recorded' if recorded else 'not recorded'}"
    assert len(fetches) == 1
    assert fetches[0][-1] == "+refs/heads/main:refs/remotes/origin/main"
    assert git(repos.clone, "rev-parse", "origin/main") == rewritten
    assert git(repos.clone, "rev-parse", "HEAD") == repos.base
    assert repos.log.read_text(encoding="utf-8") == repos.initial


def test_failed_fetch_is_named_and_never_reads_stale_file(data_repos, monkeypatch):
    repos = data_repos
    assert verify(repos.ctx, "D-1").ok
    git(repos.clone, "remote", "set-url", "origin", str(repos.clone / "missing"))
    read_text = Path.read_text

    def no_stale_read(path, *args, **kwargs):
        assert path != repos.log, "stale decisions file was read"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_stale_read)
    result = verify(repos.ctx, "D-1")
    assert not result.ok
    assert result.detail == "could not read the decisions file (DecisionsFetchFailed)"
    assert git(repos.clone, "show", f"origin/main:{repos.relative}") == repos.initial.strip()


def test_unpushed_working_tree_row_is_not_evidence(data_repos):
    repos = data_repos
    repos.log.write_text(repos.initial + "| D-5571 | demo | Local only |\n")
    result = verify(repos.ctx)
    assert not result.ok
    assert result.detail == "decision D-5571 is not recorded"


def test_origin_read_does_not_require_local_log(data_repos):
    repos = data_repos
    repos.log.unlink()
    result = verify(repos.ctx, "D-1")
    assert result.ok, result.detail
    assert not repos.log.exists()


def test_configured_file_may_be_in_a_worktree(data_repos):
    repos = data_repos
    push_decision(repos)
    ctx = evidence.default_context({
        "decisions_file": str(repos.worktree / repos.relative)})
    result = verify(ctx)
    assert result.ok, result.detail
    assert git(repos.clone, "rev-parse", "HEAD") == repos.base


@pytest.mark.parametrize("caller_setting", [
    "repository-env", "indexed-config", "repository-indexed-config", "global-config", "system-config",
])
def test_origin_read_ignores_ambient_repository_and_config(data_repos, tmp_path, monkeypatch, caller_setting):
    repos = data_repos
    other = tmp_path / "other"
    git(tmp_path, "clone", str(repos.origin), str(other))
    push_decision(repos)
    git(repos.clone, "update-ref", "refs/remotes/origin/main", repos.base)
    if "repository" in caller_setting:
        for name, value in {
            "GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
            "GIT_INDEX_FILE": str(other / ".git/index"),
            "GIT_OBJECT_DIRECTORY": str(other / ".git/objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(other / ".git/objects"),
            "GIT_COMMON_DIR": str(other / ".git"), "GIT_NAMESPACE": "unrelated",
        }.items():
            monkeypatch.setenv(name, value)
    if "indexed" in caller_setting:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{other.as_posix()}.insteadOf")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(repos.origin))
    if caller_setting in {"global-config", "system-config"}:
        config = tmp_path / "hostile.gitconfig"
        config.write_text(
            f'[url "{other.as_posix()}"]\n\tinsteadOf = {repos.origin}\n', encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_" + caller_setting.split("-")[0].upper(), str(config))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")
    run, isolated = subprocess.run, []

    def capture_env(args, **kwargs):
        env = kwargs.get("env", {})
        isolated.append(
            {name for name in env if name.startswith("GIT_")} == {
                "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT", "GIT_ALLOW_PROTOCOL"}
            and env.get("GIT_CONFIG_NOSYSTEM") == "1"
            and env.get("GIT_CONFIG_GLOBAL") == os.devnull
            and env.get("GIT_ALLOW_PROTOCOL") == "file")
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture_env)
    result = verify(repos.ctx)
    assert result.ok, result.detail
    assert isolated == [True, True, True]
    assert repos.log.read_text(encoding="utf-8") == repos.initial


@pytest.mark.parametrize("failure", [
    "missing-ref", "missing-log", "invalid-utf8", "directory", "gitlink",
])
def test_unreadable_origin_log_is_named_without_local_fallback(data_repos, monkeypatch, failure):
    repos = data_repos
    if failure != "missing-ref":
        log = repos.worktree / repos.relative
        if failure == "missing-log":
            log.unlink()
        elif failure == "directory":
            log.unlink()
            log.mkdir()
            (log / "entry.md").write_text(repos.initial, encoding="utf-8")
            git(repos.worktree, "add", "-A")
        elif failure == "gitlink":
            git(repos.worktree, "rm", repos.relative)
            git(repos.worktree, "update-index", "--add", "--cacheinfo",
                "160000", repos.base, repos.relative)
        else:
            log.write_bytes(b"\xff\n")
        git(repos.worktree, "commit", "-am", "Make origin log unreadable")
        git(repos.worktree, "push", "origin", "HEAD:main")
    else:
        run = subprocess.run

        def missing_after_fetch(args, **kwargs):
            result = run(args, **kwargs)
            if "fetch" in args and result.returncode == 0:
                git(repos.clone, "update-ref", "-d", "refs/remotes/origin/main")
            return result

        monkeypatch.setattr(subprocess, "run", missing_after_fetch)
    result = verify(repos.ctx, "D-1")
    assert not result.ok
    assert result.detail == "could not read the decisions file (DecisionsOriginUnreadable)"
    assert repos.log.read_text(encoding="utf-8") == repos.initial


def test_real_store_attaches_decision_pushed_from_worktree(data_repos, tmp_path):
    from keeper_util import World, seed, to_claimed
    from plangraph.keeper.store import Store

    repos = data_repos
    push_decision(repos)
    git(repos.clone, "update-ref", "refs/remotes/origin/main", repos.base)
    world = World(tmp_path / "keeper")
    try:
        world.store = Store(world.conn, clock=world.clock, ctx=repos.ctx)
        (key,) = seed(world, [("A", [])], evidence_kinds=("decision",))
        epoch = to_claimed(world, key)
        result = world.store.add_evidence(
            world.p("l1main"), key, epoch, "decision", {"id": "D-5571"})
        assert result["verified"] is True, result["detail"]
        assert git(repos.clone, "rev-parse", "HEAD") == repos.base
        assert repos.log.read_text(encoding="utf-8") == repos.initial
        git(repos.clone, "remote", "set-url", "origin", str(repos.clone / "missing"))
        result = world.store.add_evidence(
            world.p("l1main"), key, epoch, "decision", {"id": "D-5571"})
        assert result["verified"] is False
        assert result["detail"] == "could not read the decisions file (DecisionsFetchFailed)"
        from plangraph.keeper.errors import Refused

        world.store.submit(world.p("l1main"), key, epoch)
        with pytest.raises(Refused) as exc:
            world.store.verdict(world.p("reviewer"), key, True, "Recheck decision")
        assert exc.value.code == "evidence_unverified"
        assert "DecisionsFetchFailed" in exc.value.msg
        assert world.store._item(key)["state"] == "in_review"
    finally:
        world.close()


def test_origin_context_does_not_cache_by_stale_checkout_metadata(data_repos):
    from plangraph.keeper.context_sources import decision_book

    repos = data_repos
    # Warm the stat cache for this exact path using the local-document adapter.
    local = evidence.DecisionsAdapter(repos.log)
    assert "D-5571" not in decision_book(local)["rows"]
    assert "D-5571" not in decision_book(repos.ctx.decisions)["rows"]
    before = repos.log.stat()
    push_decision(repos)
    assert "D-5571" in decision_book(repos.ctx.decisions)["rows"]
    assert "D-5571" not in decision_book(local)["rows"]
    after = repos.log.stat()
    assert (before.st_ino, before.st_mtime_ns, before.st_size) == (
        after.st_ino, after.st_mtime_ns, after.st_size)
    git(repos.clone, "remote", "set-url", "origin", str(repos.clone / "missing"))
    assert decision_book(repos.ctx.decisions)["rows"] == {}


def test_configured_nonrepo_file_refuses_without_fallback(data_repos):
    nonrepo = data_repos.clone.parent / "nonrepo"
    nonrepo.mkdir()
    # An invalid gitfile bounds discovery even when tmp_path is inside a repo.
    (nonrepo / ".git").write_text("gitdir: missing\n", encoding="utf-8")
    log = nonrepo / "decisions.md"
    log.write_text("| D-1 | demo | Local only |\n")
    ctx = evidence.default_context({"decisions_file": str(log)})
    result = verify(ctx, "D-1")
    assert not result.ok
    assert result.detail == "could not read the decisions file (DecisionsOriginUnreadable)"


def test_deleted_origin_main_refuses_even_with_cached_ref_and_local_row(data_repos):
    repos = data_repos
    git(repos.origin, "update-ref", "-d", "refs/heads/main")
    result = verify(repos.ctx, "D-1")
    assert not result.ok
    assert result.detail == "could not read the decisions file (DecisionsFetchFailed)"
    assert git(repos.clone, "rev-parse", "origin/main") == repos.base


def test_fetch_timeout_is_named_and_sanitised(data_repos, monkeypatch):
    run = subprocess.run

    def timeout_fetch(args, **kwargs):
        if "fetch" in args:
            assert kwargs["timeout"] == 60
            raise subprocess.TimeoutExpired(args, 60, stderr="PRIVATE ERROR")
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", timeout_fetch)
    result = verify(data_repos.ctx, "D-1")
    assert not result.ok
    assert result.detail == "could not read the decisions file (DecisionsFetchFailed)"


@pytest.mark.parametrize("caller_setting", ["global-config", "indexed-config", "repository-env"])
def test_contract_keeper_fetch_is_hermetic(data_repos, tmp_path, monkeypatch, caller_setting):
    # Exercise the real daemon, not just the fixture's Git setup commands.
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "contract"))
    import contract_harness as harness
    from contract_world import claim, evidence as attach, make_plan

    base = tmp_path / "contract"
    replacement = (tmp_path / "missing").as_posix() + "/"
    prefix = base.as_posix() + "/"
    if caller_setting == "global-config":
        config = tmp_path / "hostile.gitconfig"
        config.write_text(f'[url "{replacement}"]\n\tinsteadOf = {prefix}\n', encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    elif caller_setting == "indexed-config":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{replacement}.insteadOf")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", prefix)
        monkeypatch.setenv("GIT_CONFIG_KEY_1", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_1", "never")
        monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "https")
    else:
        monkeypatch.setenv("GIT_DIR", str(data_repos.clone / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(data_repos.clone))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    popen, guards = subprocess.Popen, []

    def capture_guards(args, **kwargs):
        if "plangraph.keeper.daemon" in args:
            guards.append(tuple(kwargs["env"].get(name) for name in (
                "GIT_TERMINAL_PROMPT", "GIT_ALLOW_PROTOCOL")))
        return popen(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", capture_guards)
    keeper = harness.start_local(base)
    try:
        key = make_plan(keeper).key
        epoch = claim(keeper, key)
        result = attach(keeper, key, epoch)
        assert result["verified"] is True, result["detail"]
        assert guards == [("0", "file")]
    finally:
        keeper.stop()
