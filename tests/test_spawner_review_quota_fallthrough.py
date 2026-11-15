"""Review quota refusals on a clean exit fall through to the next leg in the same run (LE-REVIEW-QUOTA-NO-WAIT)."""
import pytest

from tests.test_spawner_seat_fallthrough import recorded_plan, replay  # noqa: F401  (pytest fixtures)


@pytest.mark.parametrize('pinned', [False, True])
def test_review_quota_on_clean_exit_falls_through_in_the_same_run(monkeypatch, replay, pinned):
    """A review leg that exits 0 with a quota refusal is skipped for the next leg in the same run, unless pinned."""
    quota = 'RESOURCE_EXHAUSTED (code 429): Individual quota reached'
    async def admitted(*args, **kwargs): return True
    monkeypatch.setattr('plangraph.spawner.service.admit_base', admitted)
    monkeypatch.setattr('plangraph.spawner.runner.admit_base', admitted)
    first, second = replay.plan.legs[:2]
    result = replay.run(succeed=second.id, no_fallthrough=pinned, failures={first.id: quota}, seat=True)
    assert result['status'] == ('failed' if pinned else 'completed'), result
    assert len(set(replay.run_ids)) == 1
    assert len(replay.calls) == (1 if pinned else 2)
    assert any(quota in (a.get('skip_reason') or '') for a in result['attempts'])
    if not pinned:
        assert result['served_model'] == second.model
        assert result['plan']['seat_coverage'][0]['leg'] == second.id
