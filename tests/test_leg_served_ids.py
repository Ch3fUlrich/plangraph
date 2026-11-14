"""DeepSeek registry identities and measured OpenCode Go model IDs."""

import asyncio
import subprocess
from dataclasses import replace

import pytest

from plangraph.planner.legs import DEFAULT_LEGS
from plangraph.planner.routing import SpawnPlan
from plangraph.routing_support import opencode_overlay, resolver


# Measured with `opencode models` on 2026-11-06 (D-3952).
SERVED_OPENCODE_GO_IDS = frozenset({
    "opencode-go/deepseek-v4.1-flash",
    "opencode-go/gemini-4-argon",
    "opencode-go/glm-5.2",
    "opencode-go/qwen3.8-max",
    "opencode-go/muse-spark-1.3-contributor",
})


def test_default_opencode_go_legs_name_served_ids():
    legs = [leg for leg in DEFAULT_LEGS if leg.id.startswith("opencode-go-")]
    assert legs
    for leg in legs:
        assert leg.model in SERVED_OPENCODE_GO_IDS, (leg.id, leg.model)


def test_deepseek_review_registry_model_ids():
    expected = {
        "omniroute-deepseek-flash": "omniroute/deepseek-v4.1-flash",
        "opencode-go-deepseek-v4-flash": "opencode-go/deepseek-v4.1-flash",
    }
    legs = {leg.id: leg for leg in DEFAULT_LEGS}
    catalog = {row["id"]: row for row in resolver.DEFAULT_FALLBACK_MODELS}
    for leg_id, model in expected.items():
        leg = legs[leg_id]
        assert leg.model == model
        assert "review" in leg.roles and leg.harness == "opencode"
        assert catalog[model]["family"] == leg.family
        rendered = SpawnPlan((leg,), (), "test", (), ()).to_dict()["legs"][0]
        assert (rendered["id"], rendered["model"], rendered["harness"]) == (
            leg_id, model, "opencode")


# DeepSeek-only projection of the recorded standalone probe, 2026-11-13.
# In particular, the corrected OmniRoute subscription id is NOT listed yet.
RECORDED_DEEPSEEK_PROBE = """console-deepseek/deepseek-flash
omniroute/deepseek-direct-flash
opencode-go/deepseek-v4-pro
opencode-go/deepseek-v4.1-flash
opencode/deepseek-v4.1-flash
opencode/deepseek-v4.1-flash-free
"""


@pytest.fixture
def recorded_probe(monkeypatch):
    monkeypatch.setattr(opencode_overlay, "_load_root_providers", lambda: ({}, None))
    calls = []

    def run(argv, **kwargs):
        assert argv[1:] == ["models", "--standalone"]
        assert kwargs["timeout"] == 10
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, RECORDED_DEEPSEEK_PROBE, "")

    with monkeypatch.context() as patch:
        patch.setattr(resolver.subprocess, "run", run)
        models = resolver._probe_served_models("{}", env={"PATH": ""})
    assert len(calls) == 1
    assert models == frozenset(RECORDED_DEEPSEEK_PROBE.splitlines())
    return models


@pytest.mark.real_probe
@pytest.mark.parametrize("model,offered", [
    ("omniroute/deepseek-flash", False),
    ("omniroute/deepseek-v4.1-flash", False),
    ("opencode-go/deepseek-v4.1-flash", True),
])
def test_deepseek_recorded_probe_filters_before_launch(recorded_probe, model, offered):
    leg = replace(next(leg for leg in DEFAULT_LEGS
                       if leg.id == "omniroute-deepseek-flash"), model=model)
    plan = SpawnPlan((leg,), (), "test", (), ())
    filtered = resolver.plan_for_launch(plan, probe=lambda _: recorded_probe)
    assert filtered.legs == ((leg,) if offered else ())
    expected = () if offered else ({
        "leg": leg.id, "reason": "not_served_on_host: " + model,
        "skip_reason": "not_served_on_host: " + model,
    },)
    assert filtered.skipped == expected
    assert plan.legs == (leg,) and not plan.skipped


@pytest.mark.real_probe
def test_unserved_deepseek_never_starts_child(recorded_probe, monkeypatch, tmp_path):
    from plangraph.spawner.runner import SpawnerRunner, spawn_service

    leg = next(leg for leg in DEFAULT_LEGS if leg.id == "omniroute-deepseek-flash")
    plan = SpawnPlan((leg,), (), "test", (), ())
    monkeypatch.setattr(resolver, "served_models", lambda *a, **kw: recorded_probe)
    launches = []

    async def launch(*args, **kwargs):
        launches.append(args)
        pytest.fail("unserved DeepSeek child started")

    service = spawn_service(SpawnerRunner(process_factory=launch), state=tmp_path)
    monkeypatch.setattr(service, "_route", lambda *a, **kw: plan)
    record = asyncio.run(service.spawn(
        card={"estimate_tokens": 100}, prompt="task", role="review",
        writer_families=("gpt",), wait_seconds=None))
    assert not launches and not service.runner.jobs
    assert record["status"] == "failed" and record["chosen_leg"] is None
    assert record["plan"]["legs"] == []
    assert record["attempts"]
    assert all(attempt["status"] == "skipped" for attempt in record["attempts"])
    assert record["attempts"][0]["skip_reason"] == "not_served_on_host: " + leg.model


def test_split_deepseek_provider_declaration_is_reported():
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / 'configuration/opencode/providers.json').read_text())
    model = config['providers']['omniroute']['models']['deepseek-v4.1-flash']
    assert model['modelID'] == 'deepseek-v4.1-flash'
    registered = next(leg for leg in DEFAULT_LEGS if leg.id == 'omniroute-deepseek-flash')
    assert registered.model == 'omniroute/' + model['modelID']
    changelog = (root / 'changelog.d/DEEPSEEK-REVIEW-LEG-SERVED.md').read_text()
    assert 'no provider settings' not in changelog
    assert 'shipped provider config' in changelog
