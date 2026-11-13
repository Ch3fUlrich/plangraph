"""Lane admission requires the complete caller brief before any dispatch."""
import asyncio
from types import SimpleNamespace

import pytest

from plangraph.planner.routing import SpawnRefused
from plangraph.spawner.prompt_guard import admit_prompt
from plangraph.spawner.service import SpawnService
from sessions_support import site, make, go, FakeRun


@pytest.mark.parametrize('prompt', ['Do task. [COMMON RULES]', '[ common rules ]',
                                   'R1/3 follows', 'R2/3: follows', 'Rules to follow'])
def test_partial_brief_refused_before_dispatch(tmp_path, monkeypatch, prompt):
    service = SpawnService(SimpleNamespace(jobs={}), tmp_path)
    async def dispatch(card=None, prompt='', role='implement'):
        raise AssertionError('partial brief dispatched')
    monkeypatch.setattr(service, '_spawn', dispatch)
    with pytest.raises(SpawnRefused, match='brief_incomplete:'):
        asyncio.run(service.spawn(card={'item_key': 'plangraph:TEST'}, prompt=prompt))


def test_expanded_brief_admitted(tmp_path, monkeypatch):
    prompt = 'Common rules: Rust only. Run affected tests. Commit with Writer and Plan-Item trailers.'
    assert admit_prompt(prompt, SpawnRefused) == prompt
    service = SpawnService(SimpleNamespace(jobs={}), tmp_path)
    async def dispatch(card=None, prompt='', role='implement'):
        return prompt
    monkeypatch.setattr(service, '_spawn', dispatch)
    assert asyncio.run(service.spawn(card={}, prompt=prompt)) == prompt
    assert admit_prompt('The implementation follows these rules.', SpawnRefused)


def test_session_prompt_file_refuses_partial_brief(site):
    from plangraph.routing_errors import ParameterRefused
    run = FakeRun()
    session = make(site, run, platform='win32', which=lambda _: 'claude.exe')
    path = site.tmp_path / 'brief.txt'
    path.write_text('[COMMON RULES]\nR1/3 follows', encoding='utf-8')
    with pytest.raises(ParameterRefused, match='brief_incomplete:'):
        go(site, session, dry_run=True, prompt=None, prompt_file=str(path))
    assert run.calls == []
