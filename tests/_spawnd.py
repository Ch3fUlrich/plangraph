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
TREE_TARGET = Path(__file__).resolve().parents[1] / 'rust/target'
_TARGET_ENV = ('CARGO_TARGET_DIR', 'CARGO_BUILD_TARGET_DIR')


def spawnd_build_command(release):
    """The build of ``pg-spawnd`` into the checkout's own target, whatever the environment says."""
    manifest = Path(__file__).resolve().parents[1] / 'rust/Cargo.toml'
    return ['cargo', 'build', '-p', 'pg-spawnd', '--manifest-path', str(manifest),
            '--target-dir', str(TREE_TARGET), '--message-format=json',
            *(['--release'] if release else [])]


def spawnd_build_env(environ):
    """*environ* without the variables that move Cargo's target directory."""
    return {key: value for key, value in environ.items() if key not in _TARGET_ENV}


def build_spawnd():
    """Build ``pg-spawnd`` for this tree and return the exact executable Cargo produced.

    The build owns ``rust/target`` of this checkout (``--target-dir``) and returns the path
    Cargo reports, never a guessed location: a target directory shared with other checkouts
    can hold another tree's binary under the same name, and a per-run preflight target is not
    visible to the tests. CI builds the release helper there first (a no-op here); elsewhere a
    debug build is used. A missing toolchain skips; a failing build fails with its diagnostics.
    """
    release = bool(os.environ.get('CI'))
    if release not in _BUILT:
        try:
            done = subprocess.run(
                spawnd_build_command(release), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, text=True, timeout=1500,
                env=spawnd_build_env(os.environ))
        except FileNotFoundError:
            if os.environ.get('CI'):
                pytest.fail('cargo is missing', pytrace=False)
            pytest.skip('cargo is missing')
        except subprocess.SubprocessError as error:
            pytest.fail(f'cargo build -p pg-spawnd: {error}', pytrace=False)
        path = artifact_executable(done.stdout.splitlines(), 'pg-spawnd') if not done.returncode else None
        if path is None or not Path(path).is_file():
            pytest.fail('cargo build -p pg-spawnd failed (exit %s):\n%s'
                        % (done.returncode, done.stderr[-4000:]), pytrace=False)
        _BUILT[release] = path
    return Path(_BUILT[release])
