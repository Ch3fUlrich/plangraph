"""One missing-helper policy for tests that execute the Rust spawner."""
import os
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
