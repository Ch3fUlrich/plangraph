"""The blocker items `plangraph land-train deploy-plan --blockers-out` writes pass keeper validation.

The fixture is the exact output of the Rust test `blocker_items_match_the_keeper_fixture`
(rust/plangraph-cli/src/land/deploy_tests.rs), so one file ties the two sides together.
"""
import json
from pathlib import Path

from keeper_util import World, defn

FIXTURE = Path(__file__).parent / "fixtures" / "deploy_blocker_items.json"


def test_the_blocker_definitions_are_accepted_by_the_keeper(tmp_path):
    world = World(tmp_path)
    items = json.loads(FIXTURE.read_text(encoding="utf-8"))["items"]
    world.store.create_items(world.p("router"), [
        defn("LESSONS-ENFORCE", project="plangraph", kind="gate"),
        defn("GOAL", ["LESSONS-ENFORCE"], project="plangraph", kind="goal")])
    world.store.create_items(world.p("router"), items)
    for raw in items:
        stored = world.store._item(f"{raw['project']}:{raw['id']}")
        assert stored["kind"] == "lane" and stored["state"] == "open"
        assert stored["priority"] == 10
