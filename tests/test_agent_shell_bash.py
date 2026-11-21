"""Agent shell commands run with bash semantics on every POSIX host (LE-AGENT-SHELL-ZSH-SEMANTICS)."""
import json
import shutil
import subprocess

import pytest

from plangraph.spawner.claude_env import AGENT_SHELL, claude_child_env, with_agent_shell
from plangraph.spawner.doctor import claude_shell

# What the Bash tool does with the commands that failed under zsh in session transcripts.
ZSH_TRAPS = [
    ('words="a b c"; n=0; for w in $words; do n=$((n+1)); done; echo $n', '3'),   # word-splitting
    ('for f in /nonexistent-dir/*.nothing; do echo "[$f]"; done', '[/nonexistent-dir/*.nothing]'),  # nomatch
    ('echo ==', '=='),                                                           # = expansion
]


@pytest.mark.skipif(shutil.which('bash') is None, reason='needs bash')
@pytest.mark.parametrize('command,expected', ZSH_TRAPS)
def test_pinned_shell_runs_zsh_trap_commands_with_bash_semantics(command, expected):
    shell = shutil.which(AGENT_SHELL[1]) or shutil.which('bash')
    result = subprocess.run([shell, '-c', command], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == expected, result


def test_claude_children_and_sessions_are_pinned_to_bash():
    assert AGENT_SHELL == ('CLAUDE_CODE_SHELL', '/bin/bash')
    env = with_agent_shell({'SHELL': '/bin/zsh'}, 'linux')
    assert env['CLAUDE_CODE_SHELL'] == '/bin/bash' and env['SHELL'] == '/bin/zsh'
    assert 'CLAUDE_CODE_SHELL' not in with_agent_shell({}, 'win32')
    # An inherited value never wins: the allowlist drops it and the pin sets it.
    pinned = with_agent_shell(claude_child_env({'CLAUDE_CODE_SHELL': '/bin/zsh', 'HOME': '/h'}), 'linux')
    assert pinned == {'HOME': '/h', 'CLAUDE_CODE_SHELL': '/bin/bash'}


@pytest.mark.parametrize('platform,pinned', [('linux', True), ('win32', False)])
def test_session_launch_env_pins_bash(platform, pinned, tmp_path, monkeypatch):
    from plangraph.spawner.sessions_agents import _AgentsMixin
    monkeypatch.setenv('CLAUDE_CODE_SHELL', '/bin/zsh')
    builder = _AgentsMixin()
    builder._platform = platform
    builder._which = lambda _: 'C:/bin/claude.exe'
    _, env = builder._build_argv('fleet', tmp_path, 'model', 'high', 'default',
                                 tmp_path / 'mcp.json', 'style', 'prompt', {})
    if pinned:
        assert env['CLAUDE_CODE_SHELL'] == '/bin/bash'
    else:
        assert env.get('CLAUDE_CODE_SHELL') == '/bin/zsh'  # left untouched on Windows


def test_doctor_reports_a_host_whose_sessions_still_run_zsh(tmp_path):
    settings = tmp_path / 'settings.json'
    zsh = {'SHELL': '/usr/bin/zsh'}
    status, detail = claude_shell(zsh, settings, 'linux')
    assert status == 'fail' and 'CLAUDE_CODE_SHELL=/bin/bash' in detail
    settings.write_text(json.dumps({'env': {'CLAUDE_CODE_SHELL': '/bin/bash'}}))
    assert claude_shell(zsh, settings, 'linux') == ('pass', 'sessions run /bin/bash')
    assert claude_shell({**zsh, 'CLAUDE_CODE_SHELL': '/bin/zsh'}, settings, 'linux')[0] == 'fail'
    settings.write_text('{not json')
    assert claude_shell({'SHELL': '/bin/bash'}, settings, 'linux')[0] == 'pass'
    assert claude_shell(zsh, settings, 'win32')[0] == 'skip'
