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


def wait_for_runtime_exit(pidfile):
    from pathlib import Path

    deadline = time.monotonic() + 2
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    pid = int(pidfile.read_text())
    while time.monotonic() < deadline:
        try:
            if '\nState:\tZ' in Path(f'/proc/{pid}/status').read_text():
                return
        except FileNotFoundError:
            return
        time.sleep(0.01)
    pytest.fail(f'audit process {pid} survived its one-second deadline')
