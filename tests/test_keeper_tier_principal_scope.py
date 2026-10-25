"""Create-time raise-only tier clamping belongs to oversight alone."""
from types import SimpleNamespace

import pytest

from keeper_util import World
from plangraph.keeper.errors import Refused


@pytest.fixture
def store(tmp_path):
    world = World(tmp_path)
    yield world.store
    world.close()


def tier_for(store, role, tier, title):
    return store._tier_for(
        {"review_tier": tier},
        {"project": "demo", "id": "LANE", "title": title,
         "track": "T0", "kind": "lane", "evidence_kinds": ["any"]},
        SimpleNamespace(role=role),
    )


def test_oversight_lowering_prevented_and_raising_allowed(store):
    # Below the project default ("Handle the secret" makes demo default to
    # high): REFUSED, never silently raised to the default (D-2458).
    with pytest.raises(Refused) as exc:
        tier_for(store, "oversight", "standard", "Handle the secret")
    assert "oversight:tier_below_default" in exc.value.msg
    assert exc.value.code == "forbidden"
    # At/above the default: honoured, exactly what the grant names.
    assert tier_for(store, "oversight", "high", "Ordinary lane") == "high"
    assert tier_for(store, "oversight", "standard", "Ordinary lane") == "standard"


def test_router_may_lower_below_default(store):
    assert tier_for(store, "router", "standard", "Handle the secret") == "standard"


def test_normal_principal_lowering_needs_router(store):
    with pytest.raises(Refused) as exc:
        tier_for(store, "l1", "standard", "Handle the secret")
    assert exc.value.code == "forbidden"
    assert exc.value.msg == "only the router may lower review_tier below its default"


def test_normal_principal_may_raise(store):
    assert tier_for(store, "l1", "high", "Ordinary lane") == "high"
