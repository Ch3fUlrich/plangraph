"""Whole-token path rules propose tags, without deciding planner card tags."""
import pytest

from plangraph.keeper.schema import RISK_TAGS
from plangraph.spawner.risk import evaluate_risk, is_critical_path, is_high_risk_path
from plangraph.spawner import risk


@pytest.mark.parametrize("path,band,tags", [
    ("docs/authors.md", "normal", []),
    ("README-authorship.md", "normal", []),
    ("docs/security-faq.md", "normal", ["auth"]),
    ("tests/test_policy.py", "normal", ["public-contract"]),
    ("src/cryptocurrency_prices.py", "normal", []),
    ("src/plangraph/keeper/engine.py", "high", ["live-step"]),
    ("src/plangraph/keeper/engine_worker.py", "high", ["live-step"]),
    ("schema/items.sql", "high", ["migration"]),
    ("migrations/001.sql", "high", ["migration"]),
    ("deploy/bootstrap.ps1", "high", ["live-step"]),
    (".github/workflows/check.yml", "high", ["live-step"]),
    ("src/secrets.py", "high", ["secrets"]),
    ("src/token_store.py", "high", ["secrets"]),
    ("src/credential.py", "high", ["secrets"]),
    ("setup.py", "high", ["live-step"]),
    ("bootstrap.sh", "high", ["live-step"]),
    ("install-agent.ps1", "high", ["live-step"]),
    ("units/agent.service", "high", ["live-step"]),
    ("src/live_worker.py", "high", ["live-step"]),
    ("src/lively.py", "normal", []),
    ("src/tokenizer.py", "normal", []),
    ("src/deployment.py", "normal", []),
    ("src/security_test.py", "normal", ["auth"]),
    ("docs/auth/example.py", "normal", ["auth"]),
    ("auth.py", "critical", ["auth"]),
    ("src/auth/login.py", "critical", ["auth"]),
    ("my_auth.py", "critical", ["auth"]),
    ("src/auth_utils.py", "critical", ["auth"]),
    ("src/crypto.py", "critical", ["secrets"]),
    ("src/policy.py", "critical", ["public-contract"]),
    ("src/security.py", "critical", ["auth"]),
    ("authors.md", "normal", []),
    ("authorization-notes.md", "normal", []),
    (r"SRC\AUTH\LOGIN.PY", "critical", ["auth"]),
    (r".GITHUB\WORKFLOWS\CI.YML", "high", ["live-step"]),
    ("auth.md", "normal", ["auth"]),
])
def test_path_rules(path, band, tags):
    result = evaluate_risk([path])
    assert result.risk == band
    assert is_critical_path(path) == (band == "critical")
    assert is_high_risk_path(path) == (band in ("high", "critical"))
    assert risk.propose_risk_tags([path]) == tags
    assert set(tags) <= set(RISK_TAGS)
    if tags:
        assert any("Proposed risk tags:" in reason for reason in result.reasons)


def test_empty_and_combined_tags():
    assert evaluate_risk([]).risk == "normal"
    assert risk.propose_risk_tags([]) == []
    assert risk.propose_risk_tags(["auth.py", "crypto.py", "auth.py"]) == ["auth", "secrets"]


@pytest.mark.parametrize("path", ["auth.md", "docs/architecture.md", "src/auth.py", "deploy/app.sh"])
def test_pure_docs_exempt_only_below_high(path):
    result = evaluate_risk([path], bucket="S0", is_docs_only=True, is_pure_doc=True)
    assert result.reviewer_count == (2 if result.risk in ("high", "critical") else 0)
    if path.endswith(".md"):
        assert result.risk in ("low", "normal")


@pytest.mark.parametrize("kwargs", [{"has_tests": False}, {"bucket": "S4"}])
def test_operational_escalation_preserved(kwargs):
    result = evaluate_risk(["schema/items.sql"], **kwargs)
    assert result.risk == "critical"
    assert result.reviewer_count >= 2
