"""Launch-spec quota refusal happens before service construction or rendering."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plangraph.spawner import launch_spec as spec


@pytest.fixture
def build_case(tmp_path, monkeypatch):
    leg = SimpleNamespace(id='claude-test', model='claude-test', roles=('review',),
                          disabled=None, valid_until=None, harness='test')
    monkeypatch.setattr(spec, 'DEFAULT_LEGS', (leg,))
    request = dict(run_id='run-' + 'a' * 32, job_id='test', role='review',
                   dry_run=True, leg={'id': leg.id}, prompt='test', card={},
                   cwd=str(tmp_path), state=str(tmp_path))
    service_calls = Mock()

    class Service:
        def __init__(self, **kwargs):
            service_calls(**kwargs)
            self.state = tmp_path
            self.runner = SimpleNamespace(spawn=self.spawn)

        def _leg_prompt(self, prompt, *args, **kwargs):
            return prompt

        async def spawn(self, **kwargs):
            self.runner._child_envs['job'] = {'PATH': '/hermetic'}
            return SimpleNamespace(events=[], command=getattr(leg, 'command', ['test']),
                                   job_id='job', task_prompt=kwargs['prompt'])

    monkeypatch.setattr(spec, 'SpawnService', Service)
    monkeypatch.setattr(spec, '_sweep', lambda *a: None)
    monkeypatch.setattr(spec.context_pack, 'annotate', lambda *a: None)
    monkeypatch.setattr(spec, 'writer_launch', lambda prompt, *a: (prompt, {}))
    monkeypatch.setattr(spec.runner, '_is_nt', lambda: False)
    monkeypatch.setattr(spec.runner, '_resolve_launch', lambda exe, *a, **kw: (exe, None))

    async def available(*a, **kw):
        return None

    monkeypatch.setattr(spec, 'host_skip_reason_async', available)
    monkeypatch.setattr(spec, 'admitted_by_render', lambda *a: False)
    return request, leg, service_calls


@pytest.mark.parametrize('dry', [False, True])
def test_build_quota_pause_and_reset(build_case, tmp_path, dry):
    request, leg, service_calls = build_case
    request['dry_run'] = dry
    (tmp_path / 'quota-pause').touch()
    with pytest.raises(spec.LaunchRefused, match='quota pause') as exc:
        spec.build(request)
    assert exc.value.kind == 'quota_pause'
    service_calls.assert_not_called()
    assert not (tmp_path / 'launch').exists()
    (tmp_path / 'quota-pause').unlink()
    result = spec.build(request)
    assert result['argv'] == ['test'] and result['prompt'] == 'test'
    assert result['stdin'] is None  # The helper has no stdin-reading contract.
    assert result['events'][-1]['event'] == 'launch_spec'
    service_calls.assert_called_once_with(state=request['state'])


def test_build_other_provider_unchanged(build_case, tmp_path):
    request, leg, service_calls = build_case
    leg.model = 'other-test'
    leg.id = request['leg']['id'] = 'other-test'
    baseline = spec.build(request)
    assert baseline['argv'] == ['test'] and baseline['stdin'] is None
    (tmp_path / 'quota-pause').touch()
    assert spec.build(request) == baseline
    assert service_calls.call_count == 2


@pytest.mark.parametrize('dry', [False, True])
def test_build_argv_helper_preserves_prompt_without_stdin(build_case, dry):
    request, leg, _ = build_case
    request.update(dry_run=dry, prompt='private task\né\n\n')
    leg.command = ['helper', '--prompt', request['prompt']]
    result = spec.build(request)
    assert result['argv'] == leg.command
    assert result['stdin'] is None and result['prompt'] == request['prompt']
    assert result['argv'].count(request['prompt']) == 1


@pytest.mark.parametrize('key', ['state', 'role'])
@pytest.mark.parametrize('paused', [False, True])
def test_build_missing_required_fields_is_invalid(build_case, tmp_path, monkeypatch, key, paused):
    from plangraph.spawner import target
    request, leg, service_calls = build_case
    if paused:
        (tmp_path / 'quota-pause').touch()
    quota_check = Mock(side_effect=AssertionError('quota check before request validation'))
    monkeypatch.setattr(target, 'quota_paused', quota_check)
    del request[key]
    with pytest.raises(spec.InvalidRequest, match='^' + key + '$'):
        spec.build(request)
    quota_check.assert_not_called()
    service_calls.assert_not_called()
