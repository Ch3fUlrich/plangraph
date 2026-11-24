"""Allowlisted environment for the claude harness child (D-3605, D-3649 fix r1).

The claude child logs in from the Claude subscription login kept under ``HOME`` /
``CLAUDE_CONFIG_DIR``.  It therefore gets an ALLOWLIST: only the names below survive and every
other variable is dropped, whatever its case or prefix.  A denylist cannot hold here: provider
routing switches (``CLAUDE_CODE_USE_*``), session and refresh tokens, cloud credentials and the
ambient credentials of the spawner host (database DSN, forge and gateway keys) reach the child
under names nobody lists in advance, and a new Claude Code release adds more.

Names are compared upper-case and ASCII-only, so ``https_proxy`` and ``HTTPS_PROXY`` are one name
and a look-alike spelling never passes.  Values are never touched or logged here.
"""
from __future__ import annotations

from typing import Mapping

#: Exact names (upper-case) the claude child may inherit.
CLAUDE_ENV_ALLOW = frozenset({
    # identity, shell and working directory (PWD: D-3505, the project dir comes from $PWD)
    'HOME', 'USER', 'LOGNAME', 'SHELL', 'PATH', 'PWD', 'TERM', 'CARGO_TARGET_DIR', 'CARGO_INCREMENTAL',
    'RUSTC_WRAPPER', 'SCCACHE_CACHE_SIZE', 'SCCACHE_DIR', 'CARGO_HOME',
    'PIP_CACHE_DIR', 'NPM_CONFIG_CACHE', 'UV_CACHE_DIR',
    # locale and time zone
    'LANG', 'LANGUAGE', 'TZ',
    # scratch space
    'TMPDIR', 'TEMP', 'TMP',
    # where the subscription login lives, and the pinned shell of the Bash tool
    'CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_SHELL',
    # network egress and the CA bundle paths it may need
    'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NODE_EXTRA_CA_CERTS',
    # Windows process basics (the same set build_child_env keeps)
    'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'COMSPEC', 'PATHEXT', 'USERPROFILE', 'APPDATA',
    'LOCALAPPDATA', 'PROGRAMDATA', 'HOMEDRIVE', 'HOMEPATH', 'USERNAME',
})

#: Name prefixes (upper-case) that are allowed whole: locale categories and XDG base directories.
CLAUDE_ENV_ALLOW_PREFIXES = ('LC_', 'XDG_')


def claude_env_allowed(name: str) -> bool:
    """True when ``name`` may reach the claude child."""
    if not isinstance(name, str) or not name.isascii():
        return False
    key = name.upper()
    return key in CLAUDE_ENV_ALLOW or key.startswith(CLAUDE_ENV_ALLOW_PREFIXES)


def claude_child_env(env: Mapping[str, str]) -> dict[str, str]:
    """A new dict holding only the allowlisted names of ``env`` (values unchanged)."""
    return {name: value for name, value in env.items() if claude_env_allowed(name)}


#: Claude Code runs its Bash tool through the login shell; zsh word-splitting, NOMATCH and `=`
#: expansion broke agent commands, so every Claude session and run the spawner starts is pinned
#: to bash on POSIX hosts.
AGENT_SHELL = ('CLAUDE_CODE_SHELL', '/bin/bash')


def with_agent_shell(env: Mapping[str, str], platform: str | None = None) -> dict[str, str]:
    """A copy of ``env`` that pins the agent's Bash tool to bash (POSIX hosts only)."""
    out = dict(env)
    import sys
    if (platform or sys.platform) != 'win32':
        out[AGENT_SHELL[0]] = AGENT_SHELL[1]
    return out


#: Extra names and prefixes the Codex review child needs on top of the claude allowlist: its own
#: login home, the gateway client key and URL, and the harness API settings. Every other name of the spawner's environment (database
#: DSN, token files, forge keys) is dropped.
CODEX_ENV_ALLOW = frozenset({'CODEX_HOME', 'AUTOOS_OMNIROUTE_KEY', 'AUTOOS_OMNIROUTE_URL'})
CODEX_ENV_ALLOW_PREFIXES = ('OPENAI_', 'OMNIROUTE_')


def codex_child_env(env: Mapping[str, str]) -> dict[str, str]:
    """A new dict holding only the names a Codex review child may inherit."""
    def allowed(name):
        key = name.upper() if isinstance(name, str) and name.isascii() else ''
        return claude_env_allowed(name) or key in CODEX_ENV_ALLOW or key.startswith(CODEX_ENV_ALLOW_PREFIXES)
    return {name: value for name, value in env.items() if allowed(name)}


def isolated_child_env(cmd: list, env: Mapping[str, str]) -> Mapping[str, str]:
    """The child environment of a harness run, with its argv made to read no secret path.

    A claude child gets its allowlisted environment (D-3605, D-3649), the pinned agent shell and
    Read deny rules on the secret paths; a codex child gets the shared deny list as a read-only
    permission profile, a core-only tool environment and its allowlisted environment. Other
    harnesses are returned unchanged. ``cmd`` is edited in place."""
    if cmd[0] == 'claude':
        from plangraph.routing_support.claude_seat import deny_secret_reads
        env = with_agent_shell(claude_child_env(env))
        deny_secret_reads(cmd, env)
    elif cmd[0] == 'codex' and cmd[1:2] == ['exec']:
        from plangraph.routing_support.clients import codex_isolation_args
        cmd[2:2] = codex_isolation_args(env)
        env = codex_child_env(env)
    return env
