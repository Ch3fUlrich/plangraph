"""One missing-helper policy for tests that execute the Rust spawner."""
import os

import pytest

from plangraph import fleet_snapshot as fleet


def require_spawnd():
    try:
        return fleet._projection_binary()
    except RuntimeError as error:
        if str(error) != fleet.PG_SPAWND_MISSING:
            raise
    if os.environ.get('CI'):
        pytest.fail(fleet.PG_SPAWND_MISSING, pytrace=False)
    pytest.skip(fleet.PG_SPAWND_MISSING)
