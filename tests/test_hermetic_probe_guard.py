"""Host probes require explicit opt-in, including the async spawn path."""
import asyncio
from types import SimpleNamespace

import pytest

from plangraph.spawner import resolver


def test_unmarked_host_probe_fails_closed():
    with pytest.raises(pytest.fail.Exception, match=r'@pytest.mark.real_probe'):
        resolver._probe_served_models('{}')


def test_async_probe_uses_hermetic_stub(monkeypatch):
    monkeypatch.setattr(resolver.subprocess, 'run', lambda *a, **kw: pytest.fail('host binary reached'))
    assert asyncio.run(resolver.served_models_async('{}')) is None


@pytest.mark.real_probe
def test_marked_real_probe_keeps_mocked_subprocess(real_served_models, monkeypatch):
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='fixture/model\n')
    monkeypatch.setattr(resolver.subprocess, 'run', run)
    assert real_served_models('{}') == {'fixture/model'}
    assert asyncio.run(resolver.served_models_async('{}')) == {'fixture/model'}
    assert len(calls) == 1
