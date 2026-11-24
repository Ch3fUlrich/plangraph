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
