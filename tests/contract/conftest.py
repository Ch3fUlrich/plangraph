"""Fixtures for the keeper contract suite (see ``contract_harness`` for modes)."""
import pytest

import contract_harness as harness


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        fspath = str(getattr(item, "fspath", "") or getattr(item, "path", ""))
        if "tests/contract" in fspath.replace("\\", "/"):
            item.add_marker(pytest.mark.xdist_group("contract"))


@pytest.fixture(scope="session")
def kp(tmp_path_factory):
    """The keeper under test: ``PG_CONTRACT_URL`` if set, else a local daemon."""
    external = harness.from_env()
    if external is not None:
        missing = [n for n in harness.PRINCIPALS if n not in external.tokens]
        if missing:
            pytest.fail(f"PG_CONTRACT_URL set but tokens missing for {missing}")
        yield external
        return
    keeper = harness.start_local(tmp_path_factory.mktemp("keeper") / "k")
    try:
        yield keeper
    finally:
        keeper.stop()
