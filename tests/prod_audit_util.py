"""Bounded observation of detached audit writes in disposable repositories."""
import json
import time

import pytest


def wait_for_audit(state, predicate, description):
    deadline = time.monotonic() + 5
    rows = []
    while True:
        try:
            rows = [json.loads(line) for line in
                    (state / 'head-moves.jsonl').read_text().splitlines()]
        except (FileNotFoundError, json.JSONDecodeError):
            rows = []
        if predicate(rows):
            return rows
        if time.monotonic() >= deadline:
            pytest.fail(f'Audit did not {description} within 5 seconds; rows={rows!r}')
        time.sleep(0.01)
