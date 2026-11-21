"""One missing-helper policy for tests that execute the Rust spawner."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from plangraph import fleet_snapshot as fleet


def built_rust_binary(name, profile='debug', *, cargo_cwd=None):
    """Resolve Cargo output; relative overrides use the build command's cwd."""
    workspace = Path(__file__).resolve().parents[1] / 'rust'
    override = os.environ.get('CARGO_TARGET_DIR')
    target = Path(override) if override is not None else workspace / 'target'
    if not target.is_absolute():
        target = (Path(cargo_cwd) if cargo_cwd is not None else Path.cwd()) / target
    suffix = '.exe' if os.name == 'nt' else ''
    return target / profile / (name + suffix)


def require_spawnd():
    try:
        return fleet._projection_binary()
    except RuntimeError as error:
        if str(error) != fleet.PG_SPAWND_MISSING:
            raise
    if os.environ.get('CI'):
        pytest.fail(fleet.PG_SPAWND_MISSING, pytrace=False)
    pytest.skip(fleet.PG_SPAWND_MISSING)


def artifact_executable(lines, name):
    """The executable Cargo reported for binary *name* in ``--message-format=json`` lines."""
    found = None
    for line in lines:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        target = message.get('target') or {}
        if (message.get('reason') == 'compiler-artifact' and target.get('name') == name
                and 'bin' in target.get('kind', []) and message.get('executable')):
            found = message['executable']
    return found


_BUILT = {}


def build_spawnd():
    """Build ``pg-spawnd`` for this tree and return the exact executable Cargo produced.

    The build uses the tree's own ``rust/target`` and returns the path Cargo reports, never a
    guessed location: a target directory shared with other checkouts can hold another tree's
    binary under the same name, and a per-run preflight target is not visible to the tests.
    CI builds the release helper there first (a no-op here); elsewhere a debug build is used.
    """
    profile = ['--release'] if os.environ.get('CI') else []
    key = tuple(profile)
    if key not in _BUILT:
        manifest = Path(__file__).resolve().parents[1] / 'rust/Cargo.toml'
        try:
            done = subprocess.run(
                ['cargo', 'build', '-p', 'pg-spawnd', '--manifest-path', str(manifest),
                 '--message-format=json', *profile], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True, timeout=1500,
                env={k: v for k, v in os.environ.items() if k != 'CARGO_TARGET_DIR'})
            path = artifact_executable(done.stdout.splitlines(), 'pg-spawnd') if not done.returncode else None
        except (OSError, subprocess.SubprocessError):
            path = None
        if path is None or not Path(path).is_file():
            if os.environ.get('CI'):
                pytest.fail('cargo build -p pg-spawnd failed', pytrace=False)
            pytest.skip('cargo build -p pg-spawnd failed or cargo is missing')
        _BUILT[key] = path
    return Path(_BUILT[key])
