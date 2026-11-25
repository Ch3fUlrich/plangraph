"""Whole-token path rules propose tags, without deciding planner card tags."""
import importlib.util
from pathlib import Path

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
    ("src/" + "cred" + "ential.py", "high", ["secrets"]),
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


# Rule 3.3 classes (one row per fixture): a one-file S0 card with tests and no risk tags is at
# least high. Every row of RULE_33_CLASSES must be hit by a fixture below.
RULE_33_FIXTURES = [
    ("keeper-service-api", "src/plangraph/keeper/api_routes.py"),
    ("keeper-service-api", "keeper_service/server.py"),
    ("gate-files", "tools/ci_commit_gate.py"),
    ("gate-files", "./tools/ci_commit_gate.py"),
    ("gate-files", "tools/./ci_commit_gate.py"),
    ("gate-files", "tools/gate_wiring_check.py"),
    ("gate-files", "tools/ci_commit_exempt.txt"),
    ("gate-files", "rust/plangraph-cli/src/land/go_verify.rs"),
    ("gate-files", "rust/plangraph-cli/src/land/accept.rs"),
    ("gate-files", "rust/plangraph-cli/src/land/cli.rs"),
    ("gate-files", "rust/plangraph-cli/src/land/chain.rs"),
    ("gate-files", "rust/plangraph-cli/src/land/main_state.rs"),
    ("gate-files", "src/plangraph/spawner/opencode_isolation.py"),
    ("gate-files", "src/plangraph/spawner/runner.py"),
    ("gate-files", "src/plangraph/spawner/service.py"),
    ("gate-files", "src/plangraph/spawner/prompt_input.py"),
    ("gate-files", "src/plangraph/review/family.py"),
    ("gate-files", "rust/worker-core/src/writer_families.rs"),
    ("gate-files", "src/plangraph/spawner/git_guard.py"),
    ("gate-files", "tools/seat_verdict.py"),
    ("gate-files", "tools/seat_record.py"),
    ("gate-files", "src/plangraph/routing_support/risk.py"),
    ("gateway", "src/plangraph/review/gateway.py"),
    ("deploy-config", "deployment/stack.yml"),
    ("installer", "setup/manifest.toml"),
    ("installer", "setup/install-agent.sh"),
    ("installer", "rust/plangraph-cli/src/setup_artifact.rs"),
    ("privileged-lane", "rust/pg-jail/src/lib.rs"),
    ("privileged-lane", "rust/pg-jail/src/main.rs"),
    ("privileged-lane", "rust/pg-spawnd/src/native_writer_sandbox.rs"),
    ("privileged-lane", "rust/worker-core/src/host_readiness.rs"),
    ("privileged-lane", "rust/worker-core/src/sandbox_env.rs"),
]


@pytest.mark.parametrize("cls,path", RULE_33_FIXTURES + [("auth", "src/plangraph/auth/tokens.py")])
def test_rule_33_class_is_at_least_high_for_trivial_s0(cls, path):
    result = evaluate_risk([path], bucket="S0", has_tests=True, fanout=0, risk_tags=())
    assert result.risk in ("high", "critical"), (cls, path, result.risk)
    assert result.reviewer_count >= 2


def test_rule_33_table_has_a_test_per_class():
    classes = {name for name, _, _ in risk.RULE_33_CLASSES}
    assert classes == {"keeper-service-api", "gate-files", "gateway", "deploy-config",
                       "installer", "privileged-lane"}
    for index, (name, rx, _) in enumerate(risk.RULE_33_CLASSES):
        hits = [path for cls, path in RULE_33_FIXTURES
                if cls == name and rx.search(risk._normalize_path(path))]
        assert hits, f"row {index} ({name}, {rx.pattern}) has no fixture"
        for path in hits:
            assert evaluate_risk([path], bucket="S0").risk in ("high", "critical"), path


@pytest.mark.parametrize("path", [
    "docs/KEEPER.md", "docs/deployment/notes.md", "tests/test_keeper_api.py",
    "src/plangraph/planner/cards.py", "tests/test_seat_verdict.py",
    "docs/seat_verdict.md",
])
def test_rule_33_table_leaves_docs_tests_and_unplaced_paths_low(path):
    assert evaluate_risk([path], bucket="S0", has_tests=True).risk == "low"


@pytest.mark.parametrize("path", [
    "rust/plangraph-cli/src/land/go_verify_tests.rs",
    "rust/plangraph-cli/src/land/registrar_tests.rs",
    "rust/plangraph-cli/src/land/resolve_tests.rs",
    "rust/worker-core/src/writer_families_tests.rs",
])
def test_rule_33_table_leaves_rust_test_modules_low(path):
    assert evaluate_risk([path], bucket="S0", has_tests=True).risk == "low"


def test_rule_33_rust_test_exclusion_keeps_the_policy_token_rule():
    path = "rust/pg-spawnd/src/native_writer_policy_tests.rs"
    assert evaluate_risk([path], bucket="S0").risk == "critical"
    assert is_critical_path(path)


def test_rule_33_table_covers_every_gate_path():
    spec = importlib.util.spec_from_file_location(
        "ci_commit_gate", Path(__file__).resolve().parents[1] / "tools" / "ci_commit_gate.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    for pattern in gate.GATE_PATHS:
        path = pattern.replace("*/", "sub/", 1) if pattern.startswith("*/") else pattern
        path = path[:-1] + ("x.yml" if path.endswith("/*") else "") if path.endswith("*") else path
        assert evaluate_risk([path], bucket="S0").risk in ("high", "critical"), (pattern, path)


def test_rule_33_class_spellings_of_one_path_agree():
    spellings = ["tools/ci_commit_gate.py", "./tools/ci_commit_gate.py",
                 "tools/./ci_commit_gate.py", r"tools\ci_commit_gate.py"]
    assert {evaluate_risk([p], bucket="S0").risk for p in spellings} == {"high"}
