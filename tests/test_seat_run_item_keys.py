"""A seat started for a part of several items records every item key and the part head."""
import json

import pytest

from plangraph.spawner.item_key import annotate_item, part_keys

A, B = 'plangraph:LE-ONE', 'plangraph:LE-TWO'
HEAD = 'a' * 40


def seat_record(card):
    record = {'role': 'review'}
    annotate_item(record, card)
    return record


def test_a_two_key_part_records_both_keys_and_the_head():
    record = seat_record({'item_keys': [A, B], 'diff_ref': {'repo': '/r', 'base': 'b' * 40, 'head': HEAD}})
    assert record['item_key'] == A and record['item_keys'] == [A, B]
    assert record['request']['item_keys'] == [A, B] and record['request']['review_head'] == HEAD
    assert 'events' not in record


def test_a_named_item_key_stays_first_and_keys_are_not_repeated():
    record = seat_record({'item_key': B, 'item_keys': [A, B, A]})
    assert record['item_key'] == B and record['item_keys'] == [B, A]


def test_invalid_part_keys_are_dropped_and_a_single_key_adds_no_list():
    assert part_keys({'item_keys': [A, 'garbage', 12, B]}) == [A, B]
    assert part_keys({'item_keys': 'x'}) == [] and part_keys({}) == []
    assert 'item_keys' not in seat_record({'item_key': A})


def test_the_keeper_finds_a_seat_of_a_two_key_part_by_each_key(tmp_path):
    from plangraph.keeper.evidence_spawner import _record_keys
    record = seat_record({'item_keys': [A, B]})
    assert {A, B} <= _record_keys(record) and 'plangraph:OTHER' not in _record_keys(record)
    assert _record_keys({"item_key": A}) == {A}


@pytest.mark.parametrize('key,ok', [(A, True), (B, True), ('plangraph:OTHER', False)])
def test_the_seat_poster_accepts_every_key_of_the_part(tmp_path, key, ok):
    from test_seat_record_fixes import rec_dir
    import sys
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).parents[1] / 'tools'))
    import seat_verdict as sv
    seat = rec_dir(tmp_path, 'run-' + '1' * 32, item_key=A, item_keys=[A, B])
    if ok:
        assert sv.read_record_identity(seat, key)['item_key'] == A
    else:
        with pytest.raises(ValueError, match='does not match'):
            sv.read_record_identity(seat, key)


def test_the_seat_poster_posts_a_seat_of_a_part_under_its_second_key(tmp_path, capsys):
    """evidence_from_record copies item_key; the poster must still accept the part's other key."""
    import json
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parents[1] / 'tools'))
    import seat_verdict as sv
    from test_seat_record_fixes import HEAD40, rec_dir
    seat = rec_dir(tmp_path, 'run-' + '1' * 32, item_key=A, item_keys=[A, B])
    argv = ['--key', B, '--seat-dir', str(seat), '--writer-family', 'deepseek',
            '--token-dir', str(tmp_path), '--dry-run', '--head', HEAD40]
    assert sv.main(argv) == 0
    assert json.loads(capsys.readouterr().out)['body']['key'] == B
    argv[1] = 'plangraph:OTHER'
    assert sv.main(argv) == sv.EXIT_REFUSED


def test_the_run_record_verifier_accepts_every_key_of_a_part():
    from plangraph.keeper.evidence_spawner import _record_keys
    record = seat_record({'item_keys': [A, B]})
    assert B in _record_keys(record) and A in _record_keys(record)


def test_backfill_discovery_finds_a_part_seat_under_its_second_key(tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parents[1] / 'tools'))
    import seat_record as sr
    from test_seat_backfill import ITEM, record
    record(tmp_path, item_key=A, item_keys=[A, B])
    seats, reasons = sr.eligible_records(dict(ITEM, key=B), tmp_path)
    assert len(seats) == 1, reasons
    assert sr.eligible_records(dict(ITEM, key='plangraph:OTHER'), tmp_path)[0] == []


def test_backfill_discovery_accepts_a_part_prompt_naming_every_key_of_the_part(tmp_path):
    """The review brief names the part's first key; known_keys holds every in-review key."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parents[1] / 'tools'))
    import seat_record as sr
    from test_seat_backfill import ITEM, record
    run = record(tmp_path, item_key=A, item_keys=[A, B])
    (run / 'prompt.txt').write_text(f'Review the part. Item key: {A}\nAlso covers {B}.')
    for key in (A, B):
        seats, reasons = sr.eligible_records(dict(ITEM, key=key), tmp_path, known_keys={A, B})
        assert len(seats) == 1, (key, reasons)
    # A key outside the part still refuses the seat.
    (run / 'prompt.txt').write_text(f'Review {A} and {B} and plangraph:STRANGER')
    seats, reasons = sr.eligible_records(dict(ITEM, key=B), tmp_path, known_keys={A, B, 'plangraph:STRANGER'})
    assert seats == [] and any('item key mismatch' in r for r in reasons)
