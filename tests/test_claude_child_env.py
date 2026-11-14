"""D-3605/D-3649: the claude child gets an ALLOWLISTED environment (hermetic).

Every assertion here is on variable NAMES.  A failing assert must never print the child env, because
the failure lands in public CI logs: values only ever appear as booleans (``home_ok``), and
``test_failure_message_carries_names_only`` pins the helper that builds the message.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import replace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import plan_spawn
from plangraph.routing_support import resolver
from plangraph.spawner import claude_env
from plangraph.spawner.runner import SpawnerRunner

from registry_route_util import CARD, live_policy  # noqa: F401

#: The policy restated literally.  Widening claude_env.CLAUDE_ENV_ALLOW has to change this list too.
POLICY = (
    'APPDATA', 'CARGO_TARGET_DIR', 'CLAUDE_CONFIG_DIR', 'COMSPEC', 'HOME', 'HOMEDRIVE', 'HOMEPATH', 'HTTPS_PROXY', 'HTTP_PROXY',
    'LANG', 'LANGUAGE', 'LOCALAPPDATA', 'LOGNAME', 'NODE_EXTRA_CA_CERTS', 'NO_PROXY', 'PATH', 'PATHEXT',
    'PROGRAMDATA', 'PWD', 'SHELL', 'SSL_CERT_DIR', 'SSL_CERT_FILE', 'SYSTEMDRIVE', 'SYSTEMROOT', 'TEMP',
    'TERM', 'TMP', 'TMPDIR', 'TZ', 'USER', 'USERNAME', 'USERPROFILE', 'WINDIR',
)
PREFIXES = ('LC_', 'XDG_')

#: Names that must never reach the claude child: provider switches, session and refresh tokens, cloud and
#: forge credentials, the host's database DSN and gateway keys, plus a lower-case spelling.
UNLISTED = (
    'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_MODEL', 'anthropic_api_key',
    'Anthropic_Auth_Token', 'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR',
    'CLAUDE_CODE_OAUTH_REFRESH_TOKEN', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
    'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDE_CODE_USE_MANTLE', 'CLAUDE_CODE_USE_ANTHROPIC_AWS',
    'CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD', 'CLAUDE_CODE_USE_GATEWAY', 'CLAUDE_CODE_SKIP_BEDROCK_AUTH',
    'CLAUDE_CODE_SKIP_VERTEX_AUTH', 'CLAUDE_CODE_SKIP_FOUNDRY_AUTH', 'CLAUDE_CODE_SKIP_MANTLE_AUTH',
    'CLAUDE_CODE_GATEWAY_TOKEN', 'CLAUDE_CODE_API_BASE_URL', 'CLAUDE_CODE_MESSAGING_TOKEN',
    'CLAUDE_CODE_SESSION_ACCESS_TOKEN', 'CLAUDE_CODE_SESSIONS_ACCESS_TOKEN', 'claude_code_use_mantle',
    'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE', 'AWS_BEARER_TOKEN_BEDROCK',
    'GOOGLE_APPLICATION_CREDENTIALS', 'OPENAI_API_KEY', 'OPENROUTER_API_KEY', 'GEMINI_API_KEY', 'GITHUB_TOKEN',
    'GH_TOKEN', 'NPM_TOKEN', 'HF_TOKEN', 'PG_DSN', 'PGPASSWORD', 'DATABASE_URL', 'AUTOOS_OMNIROUTE_KEY',
    'SSH_AUTH_SOCK', 'OMNIROUTE_BASE_URL', 'PLANGRAPH_GATEWAY_URL', 'PLANGRAPH_GATEWAY_LOG_TOKEN',
    'OPENCODE_CONFIG_CONTENT', 'NODE_OPTIONS', 'LD_PRELOAD', 'PYTHONPATH', 'MY_SERVICE_SECRET', 'x_unrelated',
)


def policy_allows(name):
    key = name.upper()
    return name.isascii() and (key in POLICY or key.startswith(PREFIXES))


def check_env(env):
    """Assert the child env holds allowlisted NAMES only; the message carries names, never values."""
    stray = sorted(name for name in env if not policy_allows(name))
    assert not stray, 'names outside the allowlist reached the claude child: ' + ', '.join(stray)


def fake_binary(directory, name):
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text('#!/bin/sh\nexit 0\n')
    path.chmod(0o755)
    return directory


def test_allowlist_is_pinned():
    assert sorted(claude_env.CLAUDE_ENV_ALLOW) == sorted(POLICY)
    assert claude_env.CLAUDE_ENV_ALLOW_PREFIXES == PREFIXES


def test_filter_drops_every_unlisted_name_in_any_case():
    kept = {'HOME': '/h', 'Path': '/bin', 'https_proxy': 'p', 'No_Proxy': 'q', 'LC_ALL': 'C', 'xdg_data_home': '/d',
            'CLAUDE_CONFIG_DIR': '/cfg', 'PWD': '/w', 'SystemRoot': 'r'}
    out = claude_env.claude_child_env({**dict.fromkeys(UNLISTED, 'v'), **kept})
    assert sorted(out) == sorted(kept)
    values_kept = all(out[name] == value for name, value in kept.items())
    assert values_kept


def test_lookalike_and_non_ascii_names_are_dropped():
    odd = {'ſystemroot': 'v', 'HOMEı': 'v', 'PATH ': 'v', ' PATH': 'v', 'XDG': 'v', 'LC': 'v', '': 'v'}
    assert sorted(claude_env.claude_child_env(odd)) == []


def test_failure_message_carries_names_only():
    sentinel = 'sentinel-value-0042'
    with pytest.raises(AssertionError) as caught:
        check_env({'HOME': '/h-value', 'SOME_UNLISTED_NAME': sentinel})
    text = str(caught.value)
    assert 'SOME_UNLISTED_NAME' in text
    assert sentinel not in text and '/h-value' not in text


def drive_spawn(monkeypatch, tmp_path, *, extra_env=None, path_dirs=(), system_path=True):
    """Run SpawnerRunner.spawn for the claude harness up to the exec boundary; return (env passed, job)."""
    for name in UNLISTED:
        monkeypatch.setenv(name, 'v')
    monkeypatch.setenv('HOME', '/h')
    monkeypatch.setenv('PATH', os.pathsep.join([*map(str, path_dirs), *(['/bin', '/usr/bin'] if system_path else [])]))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', '/cfg')
    monkeypatch.setenv('XDG_CONFIG_HOME', '/xdg')
    monkeypatch.setenv('LC_ALL', 'C')
    monkeypatch.setenv('https_proxy', 'p')
    monkeypatch.delenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', raising=False)
    seen = {}

    class Stop(Exception):
        pass

    async def factory(exe, *args, **kw):
        seen['env'] = dict(kw['env'])
        raise Stop

    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
    job = None
    try:
        job = asyncio.run(runner.spawn('review this', 'claude-haiku-5-5', role='review', client='claude',
                                       cwd=str(tmp_path), extra_env=extra_env))
    except Stop:
        pass
    return seen.get('env'), job


def test_spawn_path_child_env_is_exactly_the_allowlist(monkeypatch, tmp_path):
    env, _ = drive_spawn(monkeypatch, tmp_path, path_dirs=[fake_binary(tmp_path / 'bin', 'claude')],
                         extra_env={'SMUGGLED_BY_CALLER': 'v', 'XDG_DATA_HOME': '/d'})
    assert env is not None
    check_env(env)
    expected = {name for name in os.environ if policy_allows(name)} | {'PWD', 'XDG_DATA_HOME', 'CARGO_TARGET_DIR'}
    assert set(env) == expected, 'allowlisted names went missing: ' + ', '.join(sorted(expected - set(env)))
    home_ok, cfg_ok = env.get('HOME') == '/h', env.get('CLAUDE_CONFIG_DIR') == '/cfg'
    pwd_ok, xdg_ok = env.get('PWD') == str(tmp_path), env.get('XDG_DATA_HOME') == '/d'
    assert home_ok and cfg_ok and pwd_ok and xdg_ok


def test_missing_claude_binary_is_a_harness_missing_skip_not_an_exec_failure(monkeypatch, tmp_path):
    empty = tmp_path / 'empty'
    empty.mkdir()
    env, job = drive_spawn(monkeypatch, tmp_path, path_dirs=[empty], system_path=False)   # no claude: the factory must never run
    launched = env is not None   # a bool: a failing assert must not print the env
    assert not launched, 'the claude binary was launched on a host without it'
    assert job.status == 'failed' and job.stderr == 'harness_missing: claude'


def test_host_skip_reason_names_a_missing_claude_binary(monkeypatch, tmp_path):
    row = dict(model='claude-haiku-5-5', harness='claude')
    empty = tmp_path / 'empty'
    empty.mkdir()
    monkeypatch.setenv('PATH', str(empty))
    assert resolver.host_skip_reason(row) == 'harness_missing: claude'
    monkeypatch.setenv('PATH', str(fake_binary(tmp_path / 'bin', 'claude')))
    assert resolver.host_skip_reason(row) is None
    assert resolver.host_skip_reason(row, env={'PATH': str(empty)}) == 'harness_missing: claude'   # the child's PATH decides
    assert resolver.host_skip_reason(row, env={'PATH': ''}) == 'harness_missing: claude'   # empty is the child's PATH, not the host's
    monkeypatch.setattr(os, 'defpath', str(empty))   # hermetic: a host claude in /usr/bin must not decide
    assert resolver.host_skip_reason(row, env={'PATH': None}) == 'harness_missing: claude'   # removed: exec searches os.defpath
    assert resolver.host_skip_reason(row, env={'OTHER': '1'}) is None   # no PATH key: the host PATH is inherited


def test_launch_plan_skips_the_haiku_leg_on_a_host_without_claude(monkeypatch, tmp_path):
    plan = plan_spawn(CARD, role='review', writer_families=('mimo',),
                      leg_reader=lambda model, **kw: {'state': 'available', 'evidence': None})
    only = replace(plan, legs=tuple(l for l in plan.legs if l.id == 'claude-haiku-5.5'))
    assert [l.harness for l in only.legs] == ['claude']
    monkeypatch.setenv('PATH', str(tmp_path))
    launch = resolver.plan_for_launch(only)
    assert launch.legs == ()
    assert [s['reason'] for s in launch.skipped if s['leg'] == 'claude-haiku-5.5'] == ['harness_missing: claude']
    monkeypatch.setenv('PATH', str(fake_binary(tmp_path / 'bin', 'claude')))
    assert [l.id for l in resolver.plan_for_launch(only).legs] == ['claude-haiku-5.5']
    assert any(l.id == 'claude-haiku-5.5' for l in DEFAULT_LEGS)
