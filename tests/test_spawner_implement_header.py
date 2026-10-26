"""D-2966: implement prompts carry the commit-allowed header; review prompts never do."""
import asyncio

from plangraph.spawner.runner import IMPLEMENT_HEADER, SpawnerRunner


def _prompt(role, prompt='do the brief'):
    job = asyncio.run(SpawnerRunner().spawn(prompt, model_or_combo='mimo/mimo-v2.6-flash', role=role, dry_run=True))
    return job.task_prompt


def test_implement_prompt_starts_with_header():
    assert _prompt('implement') == IMPLEMENT_HEADER + '\n\ndo the brief'


def test_review_prompt_has_no_header():
    assert IMPLEMENT_HEADER not in _prompt('review')


def test_header_not_doubled_on_continuation():
    assert _prompt('implement', IMPLEMENT_HEADER + '\n\nnext round').count(IMPLEMENT_HEADER) == 1
