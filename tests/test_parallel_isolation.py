"""Pin keeper's existing worker-safe path/schema/socket isolation contracts."""
import os

import pytest

from plangraph.keeper.db_pg import derive_schema


@pytest.mark.parametrize("case", range(8))
def test_worker_paths_and_schemas(tmp_path, case):
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker:
        assert worker in str(tmp_path)
        assert f"pgtest-{worker}-" in os.environ["TMPDIR"]
    paths = [tmp_path / f"worker-{n}" / "keeper.db" for n in range(8)]
    schemas = [derive_schema(path) for path in paths]
    assert len(set(schemas)) == 8
    assert derive_schema(paths[case]) == schemas[case]


def test_daemon_ports_are_bound_not_fixed(tmp_path):
    from door_util import Doors
    from keeper_util import World

    worlds, doors = [], []
    try:
        for n in range(2):
            folder = tmp_path / str(n)
            folder.mkdir()
            world = World(folder, backend="sqlite")
            worlds.append(world)
            doors.append(Doors(world, folder))
        ports = [door.server.server_address[1] for door in doors]
        assert all(port > 0 for port in ports)
        assert len(set(ports)) == 2
        assert worlds[0].path != worlds[1].path
    finally:
        for door in doors:
            door.close()
        for world in worlds:
            world.close()
