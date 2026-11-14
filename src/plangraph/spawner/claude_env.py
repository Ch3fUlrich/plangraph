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
    'HOME', 'USER', 'LOGNAME', 'SHELL', 'PATH', 'PWD', 'TERM', 'CARGO_TARGET_DIR',
    # locale and time zone
    'LANG', 'LANGUAGE', 'TZ',
    # scratch space
    'TMPDIR', 'TEMP', 'TMP',
    # where the subscription login lives
    'CLAUDE_CONFIG_DIR',
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
