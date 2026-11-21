"""One missing-helper policy for tests that execute the Rust spawner."""
import os
from pathlib import Path
import re

import pytest

from plangraph import fleet_snapshot as fleet


def built_rust_binary(name, profile='debug', *, cargo_cwd=None, require_current=False):
    """Resolve Cargo output and optionally reject an existing stale artifact."""
    workspace = Path(__file__).resolve().parents[1] / 'rust'
    override = os.environ.get('CARGO_TARGET_DIR')
    target = Path(override) if override is not None else workspace / 'target'
    if not target.is_absolute():
        target = (Path(cargo_cwd) if cargo_cwd is not None else Path.cwd()) / target
    suffix = '.exe' if os.name == 'nt' else ''
    binary = target / profile / (name + suffix)
    if require_current and binary.is_file() and not _current_binary(binary, workspace, name):
        raise RuntimeError(
            f'stale {name}: {binary}; build {name} from this checkout before running fleet tests')
    return binary


def _current_binary(binary, workspace, name):
    # Cargo's public dep-info file includes the paths rustc used, so an artifact
    # from a different checkout is stale even when its timestamps look current.
    try:
        depfile = binary.with_name(f'{name}.d').read_text(encoding='utf-8')
        dependencies = depfile.splitlines()[0].partition(': ')[2]
        paths = {Path(part.replace(r'\ ', ' ')) for part in re.findall(r'(?:\\ |\S)+', dependencies)}
        entry = workspace / name / 'src/main.rs'
        if entry not in paths:
            return False
        stamp = binary.stat().st_mtime_ns
        return all(path.stat().st_mtime_ns <= stamp
                   for path in paths if path.is_relative_to(workspace))
    except (OSError, UnicodeError, IndexError, ValueError):
        return False


def fleet_spawnd():
    stale = None
    for profile in ('release', 'debug'):
        try:
            binary = built_rust_binary('pg-spawnd', profile, require_current=True)
        except RuntimeError as error:
            stale = error
            continue
        if binary.is_file():
            return binary
    if stale is not None:
        raise stale
    raise RuntimeError(f'build pg-spawnd before running fleet tests: {binary}')


def require_spawnd():
    try:
        return fleet._projection_binary()
    except RuntimeError as error:
        if str(error) != fleet.PG_SPAWND_MISSING:
            raise
    if os.environ.get('CI'):
        pytest.fail(fleet.PG_SPAWND_MISSING, pytrace=False)
    pytest.skip(fleet.PG_SPAWND_MISSING)
