"""D-3427: the review seat cap is enforced; a hung seat is failed/timeout and falls through."""
from types import SimpleNamespace

from plangraph.spawner.exit_outcome import fallthrough_reason, terminal_reason
from plangraph.spawner.run_watchdog import terminal_record, thresholds


def _job(status='timeout'):
    return SimpleNamespace(status=status, exit_code=124, stdout='', stderr='',
                           events=[{'event': 'watchdog_timeout'}, {'event': 'run_timeout'}])


def test_review_seat_cap_is_30_min_writer_90(monkeypatch):
    monkeypatch.delenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', raising=False)
    assert thresholds({}, 'review')['max_s'] == 30 * 60
    assert thresholds({}, 'implement')['max_s'] == 90 * 60
    assert thresholds({'max_min': 12}, 'review')['max_s'] == 12 * 60


def test_review_timeout_is_not_terminal_and_falls_through():
    record = dict(role='review')
    assert terminal_record(record, _job()) is False
    assert fallthrough_reason(None, None, _job(), record) == 'timeout'


def test_implement_timeout_stays_terminal():
    record = dict(role='implement', events=[])
    assert terminal_record(record, _job()) is True and record['reason'] == 'timeout'
    assert record['watchdog'] == {'event': 'watchdog_timeout'}   # evidence kept either way


def test_last_seat_timeout_ends_failed_timeout():
    record = dict(role='review', status='failed', exit_code=1, attempts=[{'status': 'failed', 'skip_reason': 'timeout'}])
    terminal_reason(record)
    assert record['reason'] == 'timeout'


def test_review_stall_falls_through_but_the_watchdog_only_times_out():
    # D-3427's contract covers 'stalled' as well as 'timeout'; the watchdog
    # itself never stalls a review seat (buffered harness output: only 'max'
    # applies -- see test_review_seat_only_max_rule), so the stall branch here
    # is defense in depth, not a live watchdog path.
    job = _job('stalled')
    assert terminal_record(dict(role='review'), job) is False
    assert fallthrough_reason(None, None, job, dict(role='review')) == 'stalled'
    from plangraph.spawner.run_watchdog import Watchdog
    limits = thresholds({'stall_min': 1, 'first_edit_min': 2, 'max_min': 3})
    w = Watchdog(limits, 0, ('files', 'head'), writer=False)
    assert w.decide(61, ('files', 'head'), 1, 1) is None        # never 'stalled'
    assert w.decide(3 * 60, ('files', 'head'), 1, 1)['rule'] == 'max'


def test_review_watchdog_evidence_lands_on_record():
    # D-3427: the hung review seat is not terminal, but the watchdog evidence
    # is persisted on the run record before the fallthrough continues.
    record = dict(role='review')
    assert terminal_record(record, _job()) is False
    assert record['watchdog'] == {'event': 'watchdog_timeout'}


def test_env_timeout_never_lifts_the_review_cap(monkeypatch):
    monkeypatch.setenv('PLANGRAPH_SPAWN_RUN_TIMEOUT', '7200')
    assert thresholds({}, 'review')['max_s'] == 1800 and thresholds({}, 'implement')['max_s'] == 7200


def test_no_verdict_falls_through_and_later_pass_is_clean():
    from plangraph.spawner.exit_outcome import enforce_verdict
    job = SimpleNamespace(status='completed', exit_code=0, stdout='FAILED hard', stderr='', events=[])
    enforce_verdict('review', job)
    assert job.status == 'failed'    # 'FAILED' is not a verdict line
    assert fallthrough_reason(None, None, job, dict(role='review')) == 'no_verdict'
    record = dict(status='completed', exit_code=0, events=[{'event': 'no_verdict'}], attempts=[])
    terminal_reason(record)
    assert record.get('reason') != 'no_verdict'


def test_guard_outranks_earlier_no_verdict_and_last_attempt_decides():
    record = dict(status='failed', exit_code=1, events=[{'event': 'no_verdict'}, {'event': 'writer_no_changes'}],
                  attempts=[{'skip_reason': 'no_verdict'}, {'skip_reason': 'harness exit code 1'}])
    terminal_reason(record)
    assert record['reason'] == 'writer_no_changes'
    record = dict(status='failed', exit_code=1, events=[{'event': 'no_verdict'}],
                  attempts=[{'skip_reason': 'no_verdict'}, {'skip_reason': 'harness exit code 1'}])
    terminal_reason(record)
    assert record['reason'] != 'no_verdict'


def test_review_card_cannot_lift_seat_cap():
    from plangraph.spawner.run_watchdog import thresholds
    assert thresholds({'max_min': 120}, 'review')['max_s'] == 30 * 60
