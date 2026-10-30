"""Dynamic model admission (D-3259)."""
import pytest
from plangraph.planner.routing import SpawnRefused


def test_gateway_admission():
    from plangraph.planner.model_admission import validate_model
    assert validate_model('omniroute/fresh-model', gateway_list=lambda: {'fresh-model'},
                          opencode_list=lambda: set()) == 'omniroute/fresh-model'


def test_unlisted():
    from plangraph.planner.model_admission import validate_model
    with pytest.raises(SpawnRefused, match='not in catalog, gateway list or opencode models'):
        validate_model('fixture/absent', gateway_list=lambda: set(), opencode_list=lambda: set())
