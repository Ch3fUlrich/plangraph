"""A spawner/planner core change selects every test that reaches it, with no local bound."""
from pathlib import Path

import pytest
from test_affected_tests import selector

ROOT = Path(__file__).resolve().parents[1]
CORE = ["src/plangraph/spawner/target.py", "src/plangraph/planner/legs.py",
        "src/plangraph/routing_support/liveness.py"]
# Tests that failed in train CI after fast checks selected only the first 39 reachable files.
INCIDENT = ["tests/test_opencode_isolation_go.py", "tests/test_w30_regressions.py",
            "tests/test_spawner_review_tools_inline.py", "tests/test_reconcile_leg_key_fixture.py",
            "tests/test_spawner_hardening_delivery.py"]


def build(tmp_path, count, core="src/plangraph/spawner/target.py"):
    files = {core: "VALUE = 1",
             "src/plangraph/spawner/runner.py": "from plangraph.spawner import target\n",
             **{f"tests/test_run_{i}.py": "from plangraph.spawner import runner\n" for i in range(count)}}
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.mark.parametrize("core", CORE)
def test_core_change_selects_every_transitive_test_locally(tmp_path, core):
    module = selector()
    count = module.LOCAL_TEST_LIMIT + 6
    build(tmp_path, count, core)
    (tmp_path / "src/plangraph/spawner/runner.py").write_text(
        f"from plangraph.{core.split('/')[2]} import {Path(core).stem}\n")
    plan = module.select_tests(tmp_path, [core], local=True, files_only=True)
    assert {f"tests/test_run_{i}.py" for i in range(count)} <= set(plan["tests"])
    assert "ci_deferred" not in plan


def test_other_changes_keep_the_local_bound(tmp_path):
    module = selector()
    build(tmp_path, module.LOCAL_TEST_LIMIT + 6, "src/plangraph/other.py")
    (tmp_path / "src/plangraph/spawner/runner.py").write_text("from plangraph import other\n")
    plan = module.select_tests(tmp_path, ["src/plangraph/other.py"], local=True, files_only=True)
    assert len(plan["tests"]) == module.LOCAL_TEST_LIMIT
    assert plan["ci_deferred"] == 6


@pytest.mark.parametrize("core", CORE)
def test_repository_core_change_selects_the_incident_tests(core):
    plan = selector().select_tests(ROOT, [core], local=True, files_only=True)
    missing = [name for name in INCIDENT if (ROOT / name).is_file() and name not in plan["tests"]]
    assert not missing


def test_keep_local_bound_restores_the_bound_for_the_fixed_deadline_hook(tmp_path):
    module = selector()
    build(tmp_path, module.LOCAL_TEST_LIMIT + 6)
    plan = module.select_tests(tmp_path, ["src/plangraph/spawner/target.py"], local=True,
                               files_only=True, keep_local_bound=True)
    assert len(plan["tests"]) == module.LOCAL_TEST_LIMIT and plan["ci_deferred"] == 6


def test_preflight_selection_command_keeps_the_local_bound():
    source = (ROOT / "tools/preflight.py").read_text()
    assert '"--parts-union", "--keep-local-bound", "--run"' in source
