"""Contract 99: every declared route was exercised by an authenticated call.

Only meaningful after the whole directory ran; a partial run (``-k``/one file)
skips instead of failing.
"""
from pathlib import Path

import pytest

import contract_schema as cs
from contract_harness import CALLED


def test_every_route_in_the_contract_was_called(request, kp):
    here = Path(__file__).parent
    wanted = {p.name for p in here.glob("test_*.py")}
    ran = {Path(str(item.fspath)).name for item in request.session.items}
    if not wanted <= ran:
        pytest.skip("partial run: not every contract test file was collected")
    declared = {(r["method"], r["path"]) for r in cs.CONTRACT["routes"]}
    missing = sorted(declared - CALLED)
    assert not missing, f"routes never called: {missing}"
