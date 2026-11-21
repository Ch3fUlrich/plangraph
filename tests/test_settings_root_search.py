"""The settings root search never walks out of a test's own tree into a stray .git."""
import json

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


def test_guard_reports_a_git_left_above_the_temp_dir(tmp_path):
    import _stray_git_guard as guard
    inner = tmp_path / 'tmp' / 'worker'
    inner.mkdir(parents=True)
    before = guard.temp_ancestor_gits(inner)
    assert guard.leaked_gits(before, inner) == []
    (tmp_path / 'tmp' / '.git').mkdir()
    assert guard.leaked_gits(before, inner) == [str((tmp_path / 'tmp' / '.git').resolve())]
    assert not (tmp_path / 'tmp' / '.git').exists()  # the empty stray directory is cleaned up


def test_guard_keeps_a_real_repository_it_reports(tmp_path):
    import _stray_git_guard as guard
    inner = tmp_path / 'tmp' / 'worker'
    inner.mkdir(parents=True)
    before = guard.temp_ancestor_gits(inner)
    (tmp_path / 'tmp' / '.git').mkdir()
    (tmp_path / 'tmp' / '.git' / 'HEAD').write_text('ref: refs/heads/main\n')
    assert guard.leaked_gits(before, inner)
    assert (tmp_path / 'tmp' / '.git' / 'HEAD').exists()


def test_session_ceiling_is_the_pytest_basetemp(tmp_path_factory):
    import os
    from pathlib import Path
    assert Path(os.environ['PLANGRAPH_ROOT_CEILING']) == tmp_path_factory.getbasetemp().resolve()
