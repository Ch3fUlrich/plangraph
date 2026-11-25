"""D-3605/D-3649: the claude child gets an ALLOWLISTED environment (hermetic).

Every assertion here is on variable NAMES.  A failing assert must never print the child env, because
the failure lands in public CI logs: values only ever appear as booleans (``home_ok``), and
``test_failure_message_carries_names_only`` pins the helper that builds the message.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from dataclasses import replace
from unittest.mock import Mock

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import plan_spawn
from plangraph.routing_support import resolver
from plangraph.spawner import claude_env, host_ready
from plangraph.spawner.runner import SpawnerRunner

from registry_route_util import CARD, live_policy  # noqa: F401

#: The policy restated literally.  Widening claude_env.CLAUDE_ENV_ALLOW has to change this list too.
POLICY = (
    'APPDATA', 'CARGO_HOME', 'CARGO_TARGET_DIR', 'CARGO_INCREMENTAL', 'CLAUDE_CODE_SHELL',
    'CLAUDE_CONFIG_DIR', 'COMSPEC', 'HOME', 'HOMEDRIVE', 'HOMEPATH', 'HTTPS_PROXY', 'HTTP_PROXY',
    'LANG', 'LANGUAGE', 'LOCALAPPDATA', 'LOGNAME', 'NODE_EXTRA_CA_CERTS', 'NO_PROXY',
    'NPM_CONFIG_CACHE', 'PATH', 'PATHEXT', 'PIP_CACHE_DIR', 'PROGRAMDATA', 'PWD',
    'RUSTC_WRAPPER', 'SCCACHE_CACHE_SIZE', 'SCCACHE_DIR', 'SHELL', 'SSL_CERT_DIR', 'SSL_CERT_FILE',
    'SYSTEMDRIVE', 'SYSTEMROOT', 'TEMP', 'TERM', 'TMP', 'TMPDIR', 'TZ', 'USER', 'USERNAME',
    'USERPROFILE', 'UV_CACHE_DIR', 'WINDIR',
    'LC_ADDRESS', 'LC_ALL', 'LC_COLLATE', 'LC_CTYPE', 'LC_IDENTIFICATION', 'LC_MEASUREMENT', 'LC_MESSAGES',
    'LC_MONETARY', 'LC_NAME', 'LC_NUMERIC', 'LC_PAPER', 'LC_TELEPHONE', 'LC_TIME',
    'XDG_CACHE_HOME', 'XDG_CONFIG_DIRS', 'XDG_CONFIG_HOME', 'XDG_DATA_DIRS', 'XDG_DATA_HOME',
    'XDG_RUNTIME_DIR', 'XDG_STATE_HOME',
)
PREFIXES = ()

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
    'LC_FIXTURE_PASSWORD', 'XDG_KEEPER_DSN',
)


def policy_allows(name):
    key = name.upper()
    return name.isascii() and (key in POLICY or key.startswith(PREFIXES))


def check_env(env):
    """Assert the child env holds allowlisted NAMES only; the message carries names, never values."""
    stray = sorted(name for name in env if not policy_allows(name))
    assert not stray, 'names outside the allowlist reached the claude child: ' + ', '.join(stray)


@pytest.fixture(autouse=True)
def sandbox_starts(monkeypatch):
    """The host can start Claude's sandbox unless a test says otherwise (a smoke run is not hermetic)."""
    from plangraph.routing_support import claude_seat
    monkeypatch.setattr(claude_seat, 'sandbox_available', lambda env: True)
    monkeypatch.setattr(claude_seat, 'sandbox_denies_reads', lambda env: True)


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


def drive_spawn(monkeypatch, tmp_path, *, extra_env=None, path_dirs=(), system_path=True, role='review'):
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
        job = asyncio.run(runner.spawn('review this', 'claude-haiku-5-5', role=role, client='claude',
                                       cwd=str(tmp_path), extra_env=extra_env))
    except Stop:
        pass
    return seen.get('env'), job


@pytest.mark.parametrize('role', ['review', 'implement'])
def test_cargo_target_and_shared_lease_at_spawn(monkeypatch, tmp_path, role):
    shared, leased = (str(tmp_path / name) for name in ('shared', 'leased'))
    monkeypatch.setenv('PLANGRAPH_CARGO_TARGET_DIR', shared)
    monkeypatch.setenv('CARGO_BUILD_BUILD_DIR', str(tmp_path / 'ambient'))
    shared_lease = Mock()
    cargo_lease = Mock(return_value=shared_lease)
    monkeypatch.setattr('plangraph.spawner.host_ready.cargo_lease', cargo_lease)
    env, _ = drive_spawn(monkeypatch, tmp_path, role=role,
                         path_dirs=[fake_binary(tmp_path / 'bin', 'claude')],
                         extra_env={'CARGO_TARGET_DIR': leased})
    if role == 'review':
        cargo_lease.assert_not_called()
        assert env['CARGO_TARGET_DIR'] == leased
        assert env.get('CARGO_BUILD_BUILD_DIR') == leased
    else:
        cargo_lease.assert_called_once_with()
        shared_lease.close.assert_called_once_with()
        assert env['CARGO_TARGET_DIR'] == shared
        assert 'CARGO_BUILD_BUILD_DIR' not in env


@pytest.mark.parametrize('cache_mode', ['configured', 'blank-discovered', 'blank-unavailable'])
def test_spawn_path_child_env_is_exactly_the_allowlist(monkeypatch, tmp_path, cache_mode):
    monkeypatch.setenv('RUSTC_WRAPPER', 'sccache')
    monkeypatch.setenv('SCCACHE_CACHE_SIZE', '12G')
    cache = str(tmp_path / 'sccache') if cache_mode == 'configured' else ''
    monkeypatch.setenv('SCCACHE_DIR', cache)
    monkeypatch.delenv('PLANGRAPH_SCCACHE', raising=False)
    which = host_ready.shutil.which

    def find_binary(name, *args, **kwargs):
        if os.path.basename(name) == 'sccache':
            return str(tmp_path / 'bin' / 'sccache') if cache_mode == 'blank-discovered' else None
        return which(name, *args, **kwargs)

    monkeypatch.setattr(host_ready.shutil, 'which', find_binary)
    env, _ = drive_spawn(monkeypatch, tmp_path, path_dirs=[fake_binary(tmp_path / 'bin', 'claude')],
                         extra_env={'SMUGGLED_BY_CALLER': 'v', 'XDG_DATA_HOME': '/d'})
    assert env is not None
    check_env(env)
    configured_cache_reaches_child = env.get('RUSTC_WRAPPER') == 'sccache' and env.get('SCCACHE_CACHE_SIZE') == '12G'
    assert configured_cache_reaches_child
    expected = {name for name in os.environ if policy_allows(name)} | {'PWD', 'XDG_DATA_HOME', 'CARGO_TARGET_DIR', 'CARGO_INCREMENTAL'}
    if cache_mode == 'blank-unavailable':
        expected.remove('SCCACHE_DIR')
    elif cache_mode == 'blank-discovered':
        cache_ok = env.get('SCCACHE_DIR') == str(host_ready.Path.home() / '.cache/plangraph/sccache')
        assert cache_ok
    else:
        cache_ok = env.get('SCCACHE_DIR') == cache
        assert cache_ok
    if sys.platform != 'win32':  # the runner pins the agent shell even when the parent has none
        expected.add('CLAUDE_CODE_SHELL')
        assert env['CLAUDE_CODE_SHELL'] == '/bin/bash'
    assert set(env) == expected, 'allowlisted name mismatch: ' + ', '.join(sorted(set(env) ^ expected))
    home_ok, cfg_ok = env.get('HOME') == '/h', env.get('CLAUDE_CONFIG_DIR') == '/cfg'
    pwd_ok, xdg_ok = env.get('PWD') == str(tmp_path), env.get('XDG_DATA_HOME') == '/d'
    assert home_ok and cfg_ok and pwd_ok and xdg_ok


def test_missing_claude_binary_is_a_harness_missing_skip_not_an_exec_failure(monkeypatch, tmp_path):
    empty = tmp_path / 'empty'
    empty.mkdir()
    env, job = drive_spawn(monkeypatch, tmp_path, path_dirs=[empty], system_path=False)   # no claude: the factory must never run
    launched = env is not None   # a bool: a failing assert must not print the env
    assert not launched, 'the claude binary was launched on a host without it'
    assert job.status == 'failed' and job.stderr.startswith('harness_not_found claude ')


def test_host_skip_reason_names_a_missing_claude_binary(monkeypatch, tmp_path):
    row = dict(model='claude-haiku-5-5', harness='claude')
    empty = tmp_path / 'empty'
    empty.mkdir()
    monkeypatch.setenv('PATH', str(empty))
    assert resolver.host_skip_reason(row).startswith('harness_not_found claude ')
    monkeypatch.setenv('PATH', str(fake_binary(tmp_path / 'bin', 'claude')))
    assert resolver.host_skip_reason(row) is None
    assert resolver.host_skip_reason(row, env={'PATH': str(empty)}).startswith('harness_not_found claude ')   # the child's PATH decides
    assert resolver.host_skip_reason(row, env={'PATH': ''}).startswith('harness_not_found claude ')   # empty is the child's PATH, not the host's
    monkeypatch.setattr(os, 'defpath', str(empty))   # hermetic: a host claude in /usr/bin must not decide
    assert resolver.host_skip_reason(row, env={'PATH': None}).startswith('harness_not_found claude ')   # removed: exec searches os.defpath
    assert resolver.host_skip_reason(row, env={'OTHER': '1'}) is None   # no PATH key: the host PATH is inherited


def test_launch_plan_skips_the_haiku_leg_on_a_host_without_claude(monkeypatch, tmp_path):
    plan = plan_spawn(dict(CARD, paths=['src/auth/session.rs']), role='review', writer_families=('mimo',),
                      leg_reader=lambda model, **kw: {'state': 'available', 'evidence': None})
    # Credential and authentication changes get Sonnet high under D-6550.
    only = replace(plan, legs=tuple(l for l in plan.legs if l.id == 'claude-sonnet-5-5-high'))
    assert [l.harness for l in only.legs] == ['claude']
    monkeypatch.setenv('PATH', str(tmp_path))
    launch = resolver.plan_for_launch(only)
    assert launch.legs == ()
    reasons = [s['reason'] for s in launch.skipped if s['leg'] == 'claude-sonnet-5-5-high']
    assert len(reasons) == 1 and reasons[0].startswith('harness_not_found claude ')
    monkeypatch.setenv('PATH', str(fake_binary(tmp_path / 'bin', 'claude')))
    assert [l.id for l in resolver.plan_for_launch(only).legs] == ['claude-sonnet-5-5-high']
    from plangraph.planner.legs import spawnable_legs
    assert any(l.id == 'claude-sonnet-5-5-high' for l in spawnable_legs())


def drive_args(monkeypatch, tmp_path, *, client, model, role, parent_env=None, path_bin=True):
    """Run SpawnerRunner.spawn up to the exec boundary; return (exe, args, env, job) of the harness."""
    monkeypatch.setenv('HOME', '/h')
    monkeypatch.setenv('XDG_CONFIG_HOME', '/xdg')
    monkeypatch.setenv('PG_DSN', 'fixture-parent-secret')       # a spawner secret that must not reach the run
    monkeypatch.setenv('CODEX_HOME', '/codex')
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture-key')
    monkeypatch.setenv('AUTOOS_OMNIROUTE_KEY', 'fixture-gateway-key')
    for name, value in (parent_env or {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv('PATH', os.pathsep.join(
        ([str(fake_binary(tmp_path / 'bin', client))] if path_bin else []) + ['/bin', '/usr/bin']))
    monkeypatch.delenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', raising=False)
    seen = {}

    class Stop(Exception):
        pass

    async def factory(exe, *args, **kw):
        seen.update(exe=exe, args=list(args), env=dict(kw['env']))
        raise Stop

    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
    job = None
    try:
        job = asyncio.run(runner.spawn('review this', model, role=role, client=client, cwd=str(tmp_path)))
    except Stop:
        pass
    return seen.get('exe'), seen.get('args'), seen.get('env'), job


def test_the_routed_codex_run_gets_the_deny_profile_and_no_spawner_secret(monkeypatch, tmp_path):
    _, args, env, _ = drive_args(monkeypatch, tmp_path, client='codex', model='omniroute/cx-gpt-6.2-sol-high', role='review')
    assert args is not None
    line = ' '.join(args)
    assert 'default_permissions="plangraph_review"' in line
    for path in ('~/.ssh', '~/.config/plangraph', '~/.local/share/plangraph/keeper.json', '/xdg/plangraph'):
        assert f'"{path}"="none"' in line, path
    assert 'shell_environment_policy.inherit="core"' in line and '"OPENAI_*"' in line
    assert args[0] == 'exec'
    names_only = sorted(name for name in env if name in ('PG_DSN',))
    assert names_only == [], 'a spawner secret reached the codex child: ' + ', '.join(names_only)
    assert 'CODEX_HOME' in env and 'OPENAI_API_KEY' in env and 'PATH' in env
    assert 'AUTOOS_OMNIROUTE_KEY' in env      # the gateway client key the Codex process itself needs


@pytest.mark.parametrize('role', ['implement', 'review'])
def test_every_claude_route_denies_the_built_in_read_tools_on_secret_paths(monkeypatch, tmp_path, role):
    import json
    _, args, env, _ = drive_args(monkeypatch, tmp_path, client='claude', model='claude-haiku-5-5', role=role)
    assert args is not None and args.count('--settings') == 1
    deny = json.loads(args[args.index('--settings') + 1])['permissions']['deny']
    for rule in ('Read(~/.ssh/**)', 'Read(~/.config/plangraph/**)', 'Read(~/.local/share/plangraph/keeper.json)',
                 'Read(//xdg/plangraph/**)'):
        assert rule in deny, rule
    assert 'PG_DSN' not in env


CODEX_LEG = 'omniroute/cx-gpt-6.2-sol-high'


def test_a_codex_implement_run_keeps_workspace_writes_while_a_review_stays_read_only(monkeypatch, tmp_path):
    _, review, _, _ = drive_args(monkeypatch, tmp_path, client='codex', model=CODEX_LEG, role='review')
    _, write, env, _ = drive_args(monkeypatch, tmp_path, client='codex', model=CODEX_LEG, role='implement')
    review_line, write_line = ' '.join(review), ' '.join(write)
    assert '":project_roots"' not in review_line and 'network.enabled=false' in review_line
    assert '":project_roots"={"."="write"}' in write_line and '"/tmp"="write"' in write_line
    assert 'network.enabled=true' in write_line and 'shell_environment_policy.inherit="all"' in write_line
    for line in (review_line, write_line):
        assert '"~/.ssh"="none"' in line and '"~/.local/share/plangraph/keeper.json"="none"' in line
    assert 'PG_DSN' not in env, 'a spawner secret reached the codex implement child'


def test_only_the_exact_codex_names_survive_the_process_filter(monkeypatch, tmp_path):
    lookalikes = {'OMNIROUTE_API_KEY': 'v', 'OPENAI_ORG_ID': 'v', 'OPENAI_ADMIN_KEY': 'v',
                  'OMNIROUTE_ADMIN_TOKEN': 'v', 'PG_TOKEN_FILE': 'v',
                  'LC_FIXTURE_PASSWORD': 'v', 'XDG_KEEPER_DSN': 'v'}
    _, _, env, _ = drive_args(monkeypatch, tmp_path, client='codex', model=CODEX_LEG, role='review',
                              parent_env=lookalikes)
    stray = sorted(name for name in lookalikes if name in env)
    assert stray == [], 'unlisted parent names reached the codex child: ' + ', '.join(stray)
    assert {'CODEX_HOME', 'OPENAI_API_KEY', 'AUTOOS_OMNIROUTE_KEY'} <= set(env)
    assert 'XDG_CONFIG_HOME' in env      # a real base-directory name still passes


def test_the_configured_codex_executable_is_resolved_before_the_filter_drops_nothing(monkeypatch, tmp_path):
    binary = fake_binary(tmp_path / 'elsewhere', 'codex') / 'codex'
    exe, args, _, job = drive_args(monkeypatch, tmp_path, client='codex', model=CODEX_LEG, role='review',
                                   parent_env={'PLANGRAPH_CODEX_BIN': str(binary)}, path_bin=False)
    assert exe == str(binary), (exe, job.stderr if job else None)


def test_the_codex_profile_is_valid_toml_for_a_supplementary_unicode_config_home():
    import tomllib
    from plangraph.routing_support.clients import codex_isolation_args
    xdg = '/fixture/xdg-\U0001F600-\x7f-"q"\\'
    for role in ('review', 'implement'):
        args = codex_isolation_args({'XDG_CONFIG_HOME': xdg}, role)
        tables = [tomllib.loads(value) for value in args[1:len(args):2]]      # every -c value parses
        fs = tables[1]['permissions'][next(iter(tables[1]['permissions']))]['filesystem']
        assert fs[f'{xdg}/plangraph'] == 'none'


def test_a_claude_writer_runs_its_shell_in_the_sandbox_with_the_secret_paths_unreadable(monkeypatch, tmp_path):
    import json
    _, args, _, _ = drive_args(monkeypatch, tmp_path, client='claude', model='claude-haiku-5-5', role='implement')
    settings = json.loads(args[args.index('--settings') + 1])
    sandbox = settings['sandbox']
    assert sandbox['enabled'] and sandbox['failIfUnavailable'] and not sandbox['allowUnsandboxedCommands']
    assert args[args.index('--setting-sources') + 1] == '' and sandbox['excludedCommands'] == []
    deny = sandbox['filesystem']['denyRead']
    assert {'~/.ssh', '~/.config/plangraph', '~/.local/share/plangraph/keeper.json', '/xdg/plangraph'} <= set(deny)


@pytest.mark.parametrize('starts, denies', [(False, True), (True, False)], ids=['no-sandbox', 'starts-but-hides-nothing'])
def test_a_claude_writer_route_is_refused_where_the_shell_cannot_be_sandboxed(monkeypatch, tmp_path, starts, denies):
    from plangraph.routing_support import claude_seat
    monkeypatch.setattr(claude_seat, 'sandbox_available', lambda env: starts)
    monkeypatch.setattr(claude_seat, 'sandbox_denies_reads', lambda env: denies)
    lease = Mock()
    monkeypatch.setattr('plangraph.spawner.host_ready.cargo_lease', lease)
    exe, args, _, job = drive_args(monkeypatch, tmp_path, client='claude', model='claude-haiku-5-5', role='implement')
    assert exe is None and args is None, 'the claude writer was launched without a shell sandbox'
    lease.assert_not_called()          # a refused route holds no cargo lease
    assert job.status == 'failed' and job.stderr.startswith('sandbox_unavailable:')


@pytest.mark.parametrize('role', ['review', 'implement', 'fix'])
def test_a_secret_path_that_cannot_be_written_into_the_profile_stops_the_launch(monkeypatch, role):
    from plangraph.routing_errors import SpawnRefused
    from plangraph.routing_support.clients import codex_isolation_args
    bad = os.fsdecode(b'/fixture/xdg-\xff')            # a Linux config home with a non-UTF-8 byte
    with pytest.raises(SpawnRefused, match='^sandbox_refused: .*not valid UTF-8'):
        codex_isolation_args({'XDG_CONFIG_HOME': bad}, role)


def test_a_claude_writer_ignores_user_sandbox_exclusions(monkeypatch, tmp_path):
    import json
    from plangraph.routing_support import claude_seat
    argv = ['claude', '-p', '--tools', 'Read,Bash', '--setting-sources', 'user',
            '--settings', json.dumps({'sandbox': {'excludedCommands': ['cat'], 'enabled': False}})]
    assert claude_seat.confine_writer_shell(argv, {}, 'implement', 'job') is None
    assert argv[argv.index('--setting-sources') + 1] == ''      # no user, project or local settings
    sandbox = json.loads(argv[argv.index('--settings') + 1])['sandbox']
    assert sandbox['excludedCommands'] == [] and sandbox['enabled'] is True
    assert sandbox['allowUnsandboxedCommands'] is False and sandbox['autoAllowBashIfSandboxed'] is False


def _fake_messages_api(commands, log):
    """A local Messages API: each request answers with the next Bash tool call of ``commands``
    (one call per turn, so a refusal of one cannot hide another) and, when they are used up, with
    text. ``log`` always holds the tool results shown so far. No model, no credential."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def sse(events):
        return b''.join(f'event: {name}\ndata: {json.dumps(data)}\n\n'.encode() for name, data in events)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, kind, payload):
            self.send_response(code)
            self.send_header('content-type', kind)
            self.send_header('content-length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('content-length') or 0))
            request = json.loads(body or b'{}')
            if not self.path.startswith('/v1/messages') or 'count_tokens' in self.path:
                return self.reply(200, 'application/json', b'{"input_tokens": 1}')
            results = [part for message in request.get('messages', []) if isinstance(message.get('content'), list)
                       for part in message['content'] if part.get('type') == 'tool_result']
            log[:] = [str(part.get('content')) for part in results]
            message = {'id': 'msg_f', 'type': 'message', 'role': 'assistant', 'model': request.get('model', 'm'),
                       'content': [], 'stop_reason': None, 'stop_sequence': None,
                       'usage': {'input_tokens': 1, 'output_tokens': 1}}
            tools = [tool.get('name') for tool in request.get('tools', [])]
            if not request.get('stream'):
                done = dict(message, content=[{'type': 'text', 'text': 'ok'}], stop_reason='end_turn')
                return self.reply(200, 'application/json', json.dumps(done).encode())
            if len(results) < len(commands) and 'Bash' in tools:
                events = [('message_start', {'type': 'message_start', 'message': message}),
                          ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {
                              'type': 'tool_use', 'id': f'toolu_f{len(results)}', 'name': 'Bash', 'input': {}}}),
                          ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {
                              'type': 'input_json_delta', 'partial_json': json.dumps(
                                  {'command': commands[len(results)], 'description': 'fixture read'})}}),
                          ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                          ('message_delta', {'type': 'message_delta', 'delta': {
                              'stop_reason': 'tool_use', 'stop_sequence': None}, 'usage': {'output_tokens': 5}}),
                          ('message_stop', {'type': 'message_stop'})]
            else:
                events = [('message_start', {'type': 'message_start', 'message': message}),
                          ('content_block_start', {'type': 'content_block_start', 'index': 0,
                                                   'content_block': {'type': 'text', 'text': ''}}),
                          ('content_block_delta', {'type': 'content_block_delta', 'index': 0,
                                                   'delta': {'type': 'text_delta', 'text': 'done'}}),
                          ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                          ('message_delta', {'type': 'message_delta', 'delta': {
                              'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 1}}),
                          ('message_stop', {'type': 'message_stop'})]
            self.reply(200, 'text/event-stream', sse(events))

        do_GET = do_POST

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _launch_claude_writer_against_fake_api(monkeypatch, root, *, enforce, rules=True, masks=True):
    """Launch the claude writer through SpawnerRunner.spawn, the route's own launch function, in ``root``
    as its working directory, with a user settings file that excludes ``cat`` from the sandbox. The factory
    only adds the fake endpoint the route's environment filter drops. One process reads a control fixture
    in the working directory and then a fixture under the denied config path, in two tool calls.
    ``rules=False`` leaves out the generated Read permission rules and ``masks=False`` the generated
    sandbox denyRead paths (variants of the generated settings, to tell the layers apart)."""
    import json
    import shutil
    from plangraph.routing_support import claude_seat
    home = root / 'home'
    denied = home / '.config' / 'plangraph' / 'fixture-secret'
    control = root / 'control-fixture'
    denied.parent.mkdir(parents=True, exist_ok=True)
    denied.write_text('DENIED-FIXTURE-TEXT')
    control.write_text('CONTROL-FIXTURE-TEXT')
    (home / '.claude').mkdir(exist_ok=True)
    (home / '.claude' / 'settings.json').write_text(json.dumps({'sandbox': {'excludedCommands': ['cat']}}))
    results = []
    server = _fake_messages_api([f'cat {control}', f'cat {denied}'], results)
    for name in ('XDG_CONFIG_HOME', 'CLAUDE_CONFIG_DIR'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('PATH', os.pathsep.join([str(Path(shutil.which('claude')).parent), '/usr/bin', '/bin']))
    if not enforce:       # plumbing check only: the gates are open and the run is unconfined
        monkeypatch.setattr(claude_seat, 'sandbox_available', lambda env: True)
        monkeypatch.setattr(claude_seat, 'sandbox_denies_reads', lambda env: True)
    if not rules:
        monkeypatch.setattr(claude_seat, 'read_deny_rules', lambda paths: [])
    if not masks:
        monkeypatch.setattr(claude_seat, '_credential_paths', lambda env: [])

    async def factory(exe, *args, **kw):
        kw['env'] = dict(kw['env'], ANTHROPIC_BASE_URL=f'http://127.0.0.1:{server.server_port}',
                         ANTHROPIC_API_KEY='sk-' + 'ant-fixture-not-real')
        return await asyncio.create_subprocess_exec(exe, *args, **kw)

    runner = SpawnerRunner(default_cwd=str(root), process_factory=factory)
    try:
        job = asyncio.run(runner.spawn('read the fixtures', 'claude-haiku-5-5', role='implement', client='claude',
                                       cwd=str(root), wait_seconds=90))
    finally:
        server.shutdown()
    return job, results


from plangraph.routing_support import claude_seat as _seat

#: The real gates, captured before the autouse fixture replaces them.
_REAL_SANDBOX_GATES = (_seat.sandbox_available, _seat.sandbox_denies_reads)


def _claude_sandbox_runs_here():
    import shutil
    return bool(shutil.which('claude')) and all(gate(dict(os.environ)) for gate in _REAL_SANDBOX_GATES)


def _fixture_root_outside_dev_shm():
    """A fresh directory on an ordinary file system. The sandbox builds its own /dev, so a fixture under
    /dev/shm (where a test base temp may live) is missing there for a reason that is not a denial."""
    import tempfile
    for base in ('/var/tmp', str(Path.home() / '.cache')):
        if os.path.isdir(base) and os.access(base, os.W_OK):
            return Path(tempfile.mkdtemp(prefix='plangraph-claude-fixture-', dir=base))
    return None


def test_a_launched_claude_writer_cannot_read_a_denied_fixture_under_a_user_sandbox_exclusion(monkeypatch):
    """The real-boundary test, in three launches of the real claude binary through the route's launch function.
    Each launch is one process that reads a control fixture in its working directory and then a fixture under
    the denied config path, with a user settings file that excludes cat from the sandbox.
    (1) masks and rules left out: both reads reach the shell and show their text, so the fixture is visible.
    (2) generated sandbox denyRead only, no permission rule: the control read works and the same kind of read
        of the denied fixture fails inside the shell (a cat error), not at a permission prompt: the masking.
    (3) the generated settings in full: the denied read is refused, whichever layer answers first."""
    import shutil
    if not _claude_sandbox_runs_here():
        pytest.skip('needs the claude binary and a sandbox that starts and hides a fixture (bwrap and socat); '
                    'on other hosts the route is refused, see test_a_claude_writer_is_not_started_where_enforcement_cannot_be_verified')
    root = _fixture_root_outside_dev_shm()
    if root is None:
        pytest.skip('no writable directory outside /dev/shm for the fixtures')
    try:
        with monkeypatch.context() as patch:
            job, (control, denied) = _check_two_results(
                _launch_claude_writer_against_fake_api(patch, root / 'open', enforce=True, rules=False, masks=False))
        assert 'CONTROL-FIXTURE-TEXT' in control and 'DENIED-FIXTURE-TEXT' in denied, (control, denied)
        with monkeypatch.context() as patch:
            job, (control, denied) = _check_two_results(
                _launch_claude_writer_against_fake_api(patch, root / 'masked', enforce=True, rules=False))
        assert 'CONTROL-FIXTURE-TEXT' in control, 'the launched shell did not run the permitted control read'
        assert 'DENIED-FIXTURE-TEXT' not in denied
        assert 'has been denied' not in denied and 'Permission to use' not in denied, 'a permission refusal, not masking'
        import re
        fixture = root / 'masked' / 'home' / '.config' / 'plangraph' / 'fixture-secret'
        # The cat diagnostic for the exact fixture path: a sandbox setup error cannot produce this line.
        assert re.search(rf'cat: {re.escape(str(fixture))}: (No such file or directory|Permission denied)',
                         denied), denied
        with monkeypatch.context() as patch:
            job, (control, denied) = _check_two_results(
                _launch_claude_writer_against_fake_api(patch, root / 'full', enforce=True))
        assert 'CONTROL-FIXTURE-TEXT' in control
        assert 'DENIED-FIXTURE-TEXT' not in denied and 'denied' in denied.lower()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_two_results(launched):
    job, results = launched
    assert job.status == 'completed', (job.status, job.stderr)
    assert len(results) == 2, f'expected one result per tool call, got {len(results)}'
    return job, results


def test_the_real_claude_binary_fails_closed_under_the_generated_settings_when_its_sandbox_cannot_start(monkeypatch, tmp_path):
    """The real binary, started through the route's launch function with the gates opened on purpose, must honour
    sandbox.failIfUnavailable from the generated settings: it never runs the shell, so no fixture is read."""
    import shutil
    if not shutil.which('claude') or _claude_sandbox_runs_here():
        pytest.skip('needs the claude binary on a host whose sandbox cannot start; the enforcing host runs '
                    'test_a_launched_claude_writer_cannot_read_a_denied_fixture_under_a_user_sandbox_exclusion')
    job, results = _launch_claude_writer_against_fake_api(monkeypatch, tmp_path, enforce=False)
    shown = '\n'.join([*results, job.stdout or '', job.stderr or ''])
    assert job.status == 'failed' and 'sandbox required but unavailable' in shown
    assert results == [] and 'DENIED-FIXTURE-TEXT' not in shown and 'CONTROL-FIXTURE-TEXT' not in shown


def test_a_claude_writer_is_not_started_where_enforcement_cannot_be_verified(monkeypatch, tmp_path):
    """Fail closed with the real gates (no mock): a host without socat or bwrap on its PATH never starts the seat."""
    from plangraph.routing_support import claude_seat
    monkeypatch.undo()           # the module's autouse patches would hide the real gates
    lease = Mock()
    monkeypatch.setattr('plangraph.spawner.host_ready.cargo_lease', lease)
    bare = tmp_path / 'bare'
    bare.mkdir()
    fake_binary(bare, 'claude')
    monkeypatch.setenv('HOME', '/h')
    monkeypatch.setenv('PATH', str(bare))       # claude only: no bwrap, no socat
    monkeypatch.delenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', raising=False)
    launched = []

    async def factory(exe, *args, **kw):
        launched.append(exe)
        raise AssertionError('the seat was started')

    runner = SpawnerRunner(default_cwd=str(tmp_path), process_factory=factory)
    job = asyncio.run(runner.spawn('x', 'claude-haiku-5-5', role='implement', client='claude', cwd=str(tmp_path)))
    assert not launched and job.status == 'failed' and job.stderr.startswith('sandbox_unavailable:')
    lease.assert_not_called()
