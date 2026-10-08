"""Every MCP tool maps to a verb the keeper API actually serves.

Regression: the `evidence` and `priority` tools posted to /evidence and
/priority, which do not exist (the API verbs are add_evidence / set_priority),
so workers could not attach evidence through the MCP (prox-L1, 2026-10-08).
"""
from plangraph.keeper import api_routes, mcp


def test_every_mcp_route_is_a_served_verb():
    served = api_routes.READ_VERBS | api_routes.WRITE_VERBS
    broken = {tool: verb for tool, (_m, verb) in mcp._ROUTES.items() if verb not in served}
    assert broken == {}


def test_evidence_and_priority_routes():
    assert mcp._ROUTES["evidence"] == ("POST", "add_evidence")
    assert mcp._ROUTES["priority"] == ("POST", "set_priority")
