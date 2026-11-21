"""An empty review diff fails the card once with empty_diff, before any leg is tried."""
import asyncio

import pytest

from plangraph.routing_errors import SpawnRefused
from plangraph.spawner.inline_diff import InlineDiffError, ensure_nonempty_diff
from plangraph.spawner.service import SpawnService
from registry_route_util import _Runner, _repo


def test_same_head_diff_is_empty(tmp_path):
    repo, base, head = _repo(tmp_path)
    ensure_nonempty_diff({'repo': str(repo), 'base': base, 'head': head})
    with pytest.raises(InlineDiffError, match='empty_diff'):
        ensure_nonempty_diff({'repo': str(repo), 'base': head, 'head': head})


def test_unreadable_repo_is_left_to_the_legs(tmp_path):
    ensure_nonempty_diff({'repo': str(tmp_path / 'missing'), 'base': 'a' * 40, 'head': 'b' * 40})


def test_empty_diff_fails_the_card_before_any_leg(tmp_path):
    repo, _, head = _repo(tmp_path)
    runner = _Runner()
    runner.default_cwd = str(repo)
    service = SpawnService(runner=runner, state=tmp_path / 'state')
    card = {'estimate_tokens': 100, 'diff_ref': {'repo': str(repo), 'base': head, 'head': head}}
    for seats in (False, True):
        with pytest.raises(SpawnRefused, match='empty_diff'):
            asyncio.run(service.spawn(card=card, prompt='Review this change', role='review',
                                      writer_families=('claude',), seats=seats, wait_seconds=None))
    assert runner.calls == []
