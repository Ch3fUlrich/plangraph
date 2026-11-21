"""The settings root search never walks out of a test's own tree into a stray .git."""
import json

import pytest

from plangraph.settings import review_method


def _policy(path, rounds):
    path.mkdir(parents=True, exist_ok=True)
    (path / 'policy.json').write_text(json.dumps({'review_method': {'max_rounds': rounds}}))


def test_empty_git_directory_is_not_a_repository_root(tmp_path, monkeypatch):
    outer, inner = tmp_path / 'outer', tmp_path / 'outer' / 'inner'
    (outer / '.git').mkdir(parents=True)  # stray, empty: no HEAD
    _policy(inner, 3)
    monkeypatch.setenv('PLANGRAPH_SETTINGS', 'policy.json')
    monkeypatch.delenv('PLANGRAPH_ROOT_CEILING', raising=False)
    assert review_method(root=inner).max_rounds == 3


def test_real_git_directory_and_worktree_file_are_roots(tmp_path, monkeypatch):
    monkeypatch.setenv('PLANGRAPH_SETTINGS', 'policy.json')
    monkeypatch.delenv('PLANGRAPH_ROOT_CEILING', raising=False)
    real, linked = tmp_path / 'real', tmp_path / 'linked'
    (real / '.git').mkdir(parents=True)
    (real / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')
    linked.mkdir()
    (linked / '.git').write_text('gitdir: /elsewhere\n')
    _policy(real, 5)
    _policy(linked, 6)
    assert review_method(root=real / 'sub').max_rounds == 5
    assert review_method(root=linked / 'sub').max_rounds == 6


def test_root_ceiling_stops_the_search_below_a_real_git_directory(tmp_path, monkeypatch):
    outer = tmp_path / 'outer'
    (outer / '.git').mkdir(parents=True)
    (outer / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')
    _policy(outer, 9)
    ceiling, inner = outer / 'boundary', outer / 'boundary' / 'inner'
    _policy(inner, 4)
    monkeypatch.setenv('PLANGRAPH_SETTINGS', 'policy.json')
    monkeypatch.setenv('PLANGRAPH_ROOT_CEILING', str(ceiling))
    assert review_method(root=inner).max_rounds == 4
    monkeypatch.delenv('PLANGRAPH_ROOT_CEILING')
    assert review_method(root=inner).max_rounds == 9


def test_guard_reports_a_git_left_in_the_temp_dir_and_cleans_an_empty_one(tmp_path):
    import _stray_git_guard as guard
    inner = tmp_path / 'tmp' / 'worker'
    inner.mkdir(parents=True)
    with guard.watch_stray_git('demo::clean', str(inner)):
        pass
    with pytest.raises(AssertionError, match='demo::leak left a .git'):
        with guard.watch_stray_git('demo::leak', str(inner)):
            (inner / '.git').mkdir()
    assert not (inner / '.git').exists()  # the empty stray directory is cleaned up


def test_guard_keeps_a_real_repository_it_reports(tmp_path):
    import _stray_git_guard as guard
    inner = tmp_path / 'tmp' / 'worker'
    inner.mkdir(parents=True)
    with pytest.raises(AssertionError, match='demo::repo left a .git'):
        with guard.watch_stray_git('demo::repo', str(inner)):
            (inner / '.git').mkdir()
            (inner / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')
    assert (inner / '.git' / 'HEAD').exists()


def _runs_dirs(tmp_path, monkeypatch):
    import _stray_git_guard as guard
    system_tmp = tmp_path / 'shared'
    private = system_tmp / 'private'          # TMPDIR is a private subdirectory of the shared temp root
    basetemp = private / 'pytest-of-user' / 'pytest-0'
    basetemp.mkdir(parents=True)
    monkeypatch.setattr(guard.tempfile, 'gettempdir', lambda: str(private))
    monkeypatch.setenv('PLANGRAPH_ROOT_CEILING', str(basetemp))
    monkeypatch.delenv('PYTEST_XDIST_WORKER', raising=False)  # a plain process, whatever runs this test
    return system_tmp, private, basetemp


def test_guard_fails_a_leak_in_a_directory_the_run_owns(tmp_path, monkeypatch):
    import _stray_git_guard as guard
    _, private, basetemp = _runs_dirs(tmp_path, monkeypatch)
    with pytest.raises(AssertionError, match='demo::owned left a .git outside its tmp_path'):
        with guard.watch_stray_git('demo::owned'):
            (basetemp.parent / '.git').mkdir()    # directly above the basetemp
    assert not (basetemp.parent / '.git').exists()  # the empty stray is removed
    with pytest.raises(AssertionError, match='demo::private'):
        with guard.watch_stray_git('demo::private'):
            (private / '.git').mkdir()


def test_a_shared_directory_leak_is_never_attributed_to_a_test_or_removed(tmp_path, monkeypatch):
    import _stray_git_guard as guard
    system_tmp, _, _ = _runs_dirs(tmp_path, monkeypatch)
    monkeypatch.delenv('CI', raising=False)
    monkeypatch.delenv('PLANGRAPH_STRAY_GIT_GUARD', raising=False)
    session_before = guard.shared_gits()
    # Another worker's test leaks while this watcher is open: this test neither fails nor cleans it.
    with guard.watch_stray_git('demo::bystander'):
        (system_tmp / '.git').mkdir()
    assert (system_tmp / '.git').exists()
    with pytest.warns(UserWarning, match='shared with other runs'):
        assert guard.check_shared(session_before, strict=False) == [str((system_tmp / '.git').resolve())]
    assert (system_tmp / '.git').exists()          # still there for the operator to see
    with pytest.raises(AssertionError, match='shared with other runs'):
        guard.check_shared(session_before, strict=True)
    assert guard.check_shared(guard.shared_gits(), strict=True) == []   # a pre-existing one is not new


def test_guard_strict_mode_follows_ci_and_the_switch(monkeypatch):
    import _stray_git_guard as guard
    monkeypatch.delenv('CI', raising=False)
    monkeypatch.delenv('PLANGRAPH_STRAY_GIT_GUARD', raising=False)
    assert not guard.strict_mode()
    monkeypatch.setenv('CI', 'true')
    assert guard.strict_mode()
    monkeypatch.delenv('CI')
    monkeypatch.setenv('PLANGRAPH_STRAY_GIT_GUARD', '1')
    assert guard.strict_mode()


def test_session_ceiling_is_the_pytest_basetemp(tmp_path_factory):
    import os
    from pathlib import Path
    assert Path(os.environ['PLANGRAPH_ROOT_CEILING']) == tmp_path_factory.getbasetemp().resolve()


def test_an_xdist_workers_basetemp_parent_is_shared_with_its_siblings(tmp_path, monkeypatch):
    import _stray_git_guard as guard
    private = tmp_path / 'shared' / 'private'
    basetemp = private / 'pytest-of-user' / 'pytest-0' / 'alpha'  # custom worker ids exist (--tx popen//id=alpha)
    basetemp.mkdir(parents=True)
    monkeypatch.setenv('PYTEST_XDIST_WORKER', 'alpha')
    monkeypatch.setattr(guard.tempfile, 'gettempdir', lambda: str(private))
    monkeypatch.setenv('PLANGRAPH_ROOT_CEILING', str(basetemp))
    # A sibling worker's leak in the common parent is not this worker's test's to fail or clean.
    with guard.watch_stray_git('demo::bystander'):
        (basetemp.parent / '.git').mkdir()
    assert (basetemp.parent / '.git').exists()
    assert str((basetemp.parent / '.git').resolve()) in guard.shared_gits()
    # Its own basetemp stays exact.
    with pytest.raises(AssertionError, match='demo::own'):
        with guard.watch_stray_git('demo::own'):
            (basetemp / '.git').mkdir()


def test_the_controller_checks_the_shared_root_once_after_the_workers_finish(tmp_path, monkeypatch):
    import _stray_git_guard as guard
    from types import SimpleNamespace
    system_tmp, _, _ = _runs_dirs(tmp_path, monkeypatch)
    lines = []
    reporter = SimpleNamespace(write_line=lambda text, **kw: lines.append(text))
    config = SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=lambda name: reporter))
    session = SimpleNamespace(config=config, exitstatus=0)
    plugin = guard.SharedTempCheck(strict=True)
    (system_tmp / '.git').mkdir()                  # a worker leaks after another worker already finished
    plugin.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1 and 'shared with other runs' in lines[0]
    assert (system_tmp / '.git').exists()          # reported, never removed
    quiet = guard.SharedTempCheck(strict=True)     # nothing new since this snapshot
    session.exitstatus = 0
    quiet.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 0
    worker = SimpleNamespace(config=SimpleNamespace(workerinput={}), exitstatus=0)
    plugin.pytest_sessionfinish(worker, 0)         # a worker never runs the check
    assert worker.exitstatus == 0


def test_two_workers_attribute_a_private_temp_leak_to_its_owner(tmp_path, monkeypatch):
    import _stray_git_guard as guard
    shared = tmp_path / 'shared'
    bystander, owner = shared / 'worker-a', shared / 'worker-b'
    bystander.mkdir(parents=True)
    owner.mkdir()
    monkeypatch.setenv('PYTEST_XDIST_WORKER', 'b')
    monkeypatch.setenv('PLANGRAPH_ROOT_CEILING', str(owner / 'pytest'))
    monkeypatch.setattr(guard.tempfile, 'gettempdir', lambda: str(owner))
    with pytest.raises(AssertionError, match='demo::owner left a .git'):
        with guard.watch_stray_git('demo::owner'):
            with monkeypatch.context() as worker:
                worker.setenv('PYTEST_XDIST_WORKER', 'a')
                worker.setenv('PLANGRAPH_ROOT_CEILING', str(bystander / 'pytest'))
                worker.setattr(guard.tempfile, 'gettempdir', lambda: str(bystander))
                with guard.watch_stray_git('demo::bystander'):
                    (owner / '.git').mkdir()
                assert (owner / '.git').exists()
    assert not (owner / '.git').exists()


def _check_controller_shared_worker_root(tmp_path, monkeypatch, leak_root):
    import _stray_git_guard as guard
    from types import SimpleNamespace
    with monkeypatch.context() as controller:
        _, private, _ = _runs_dirs(tmp_path, controller)
        controller.delenv('PLANGRAPH_ROOT_CEILING')  # the xdist controller does not run test fixtures
        root = tmp_path / 'controller' / 'pytest-of-user' / 'pytest-0'   # workers use root/popen-gwN
        root.mkdir(parents=True)
        lines = []
        reporter = SimpleNamespace(write_line=lambda text, **kw: lines.append(text))
        config = SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=lambda name: reporter if name != 'dsession' else object()),
                                 _tmp_path_factory=SimpleNamespace(getbasetemp=lambda: root))
        session = SimpleNamespace(config=config, exitstatus=0)
        plugin = guard.SharedTempCheck(strict=True)
        plugin.pytest_sessionstart(session)
        leak = (root if leak_root == 'basetemp' else private) / '.git'
        leak.mkdir()  # gw1 leaks into a shared tree after gw0 finishes
        plugin.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1 and str(leak.resolve()) in lines[0]
    assert leak.exists()


def test_the_controller_also_watches_the_tree_above_the_workers_basetemps(tmp_path, monkeypatch):
    _check_controller_shared_worker_root(tmp_path, monkeypatch, 'basetemp')


def test_the_controller_also_watches_its_tmpdir_with_a_separate_basetemp(tmp_path, monkeypatch):
    _check_controller_shared_worker_root(tmp_path, monkeypatch, 'tmpdir')
