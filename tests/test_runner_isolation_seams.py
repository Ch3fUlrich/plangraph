"""Runner-owned isolation dependencies must reach the extracted gate at call time."""
import json

import pytest

from plangraph.spawner import opencode_isolation as isolation
from plangraph.spawner import runner


@pytest.fixture
def review_config(tmp_path, monkeypatch):
    job = tmp_path / 'job'
    job.mkdir()
    config = job / 'empty.json'
    config.write_text('{}')
    managed = tmp_path / 'managed'
    managed.mkdir()
    monkeypatch.setattr(runner, '_opencode_managed_dir', lambda env: managed)
    env = {key: str(job) for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'OPENCODE_CONFIG_DIR')}
    env.update(OPENCODE_CONFIG=str(config), OPENCODE_DISABLE_PROJECT_CONFIG='1',
               OPENCODE_DISABLE_CLAUDE_CODE='1', OPENCODE_DISABLE_CLAUDE_CODE_SKILLS='1',
               OPENCODE_API_KEY='fixture')
    cfg = {'permission': dict.fromkeys(('*', *isolation._DENIED_TOOLS), 'deny'), 'mcp': {}, 'plugin': []}
    return cfg, env


@pytest.mark.parametrize('seam', ['_DENIED_TOOLS', '_REVIEW_OPENCODE_ENV_ALLOW', '_opencode_managed_dir'])
def test_runner_rebind_reaches_isolation_gate(review_config, tmp_path, monkeypatch, seam):
    cfg, env = review_config
    text = json.dumps(cfg)
    assert runner._opencode_tools_denied(text, env)
    if seam == '_DENIED_TOOLS':
        value = (*runner._DENIED_TOOLS, 'fixture_tool')
    elif seam == '_REVIEW_OPENCODE_ENV_ALLOW':
        value = tuple(name for name in runner._REVIEW_OPENCODE_ENV_ALLOW if name != 'OPENCODE_API_KEY')
    else:
        managed = tmp_path / 'blocked-managed'
        managed.mkdir()
        (managed / 'opencode.json').write_text('{}')
        def value(env):
            return managed
    monkeypatch.setattr(runner, seam, value)
    assert not runner._opencode_tools_denied(text, env)


def test_isolation_implementation_patch_reaches_runner_wrapper(review_config, monkeypatch):
    cfg, env = review_config
    seen = []
    def refused(config_json, child_env, **dependencies):
        seen.append((config_json, child_env, dependencies))
        return False
    monkeypatch.setattr(isolation, '_opencode_tools_denied', refused)
    assert not runner._opencode_tools_denied(json.dumps(cfg), env)
    assert seen[0][2] == {'managed_dir': runner._opencode_managed_dir,
                          'denied_tools': runner._DENIED_TOOLS,
                          'env_allow': runner._REVIEW_OPENCODE_ENV_ALLOW}


@pytest.mark.parametrize('defect', ['mcp', 'plugin', 'managed-file', 'home-outside',
                                   'config-not-empty', 'disable-project', 'unapproved-env'])
def test_dependency_inputs_do_not_disable_other_checks(review_config, tmp_path, defect):
    cfg, env = review_config
    if defect == 'mcp':
        cfg['mcp'] = {'untrusted': {}}
    elif defect == 'plugin':
        cfg['plugin'] = ['untrusted']
    elif defect == 'managed-file':
        (runner._opencode_managed_dir(env) / 'opencode.json').write_text('{}')
    elif defect == 'home-outside':
        env['HOME'] = str(tmp_path / 'outside')
    elif defect == 'config-not-empty':
        (tmp_path / 'job/empty.json').write_text('{"instructions": ["untrusted"]}')
    elif defect == 'disable-project':
        env['OPENCODE_DISABLE_PROJECT_CONFIG'] = '0'
    else:
        env['OPENCODE_UNAPPROVED'] = 'untrusted'
    assert not isolation._opencode_tools_denied(json.dumps(cfg), env,
        managed_dir=runner._opencode_managed_dir, denied_tools=runner._DENIED_TOOLS,
        env_allow=runner._REVIEW_OPENCODE_ENV_ALLOW)
