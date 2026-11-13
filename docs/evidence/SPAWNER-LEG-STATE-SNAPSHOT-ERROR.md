# Snapshot route replay: failing-first evidence

Final round D-2739/D-5715. Baseline: origin/main at
`8496ec589ff3b7f2928bd3c6c68be2382fd0a466`.

The command below archives the baseline's production code and test infrastructure,
then copies only the lane replay test and its unchanged 19-row fixture. It does
not replace production functions with lane versions. Run from the lane checkout
with pytest and pytest-xdist installed; `scratch` is any approved scratch directory.

```bash
baseline=8496ec589ff3b7f2928bd3c6c68be2382fd0a466
replay=$(mktemp -d "$scratch/origin-main-replay.XXXXXX")
git archive "$baseline" src tests tools pyproject.toml pytest.ini | tar -x -C "$replay"
mkdir -p "$replay/tests/fixtures"
cp tests/test_leg_snapshot_replay.py "$replay/tests/test_leg_snapshot_replay.py"
cp tests/fixtures/leg_snapshot_attempts.json "$replay/tests/fixtures/leg_snapshot_attempts.json"
cd "$replay"
python3 -m pytest -q tests/test_leg_snapshot_replay.py --tb=short
```

Recorded output (repeated full attempt arrays omitted; assertion and summary
lines below are verbatim). Exit status: **1**.

```text
FFF                                                                      [100%]
=================================== FAILURES ===================================
_______ test_recorded_snapshot_attempt_order_retries_and_probes[gemini] ________
tests/test_leg_snapshot_replay.py:94: in test_recorded_snapshot_attempt_order_retries_and_probes
    assert probes == [leg.id for leg in expected], (
E   assert [] == ['agy-gemini-3.8-flash']
_______ test_recorded_snapshot_attempt_order_retries_and_probes[sonnet] ________
tests/test_leg_snapshot_replay.py:94: in test_recorded_snapshot_attempt_order_retries_and_probes
    assert probes == [leg.id for leg in expected], (
E   assert [] == ['agy-gemini-...y-sonnet-5.5']
_______ test_recorded_snapshot_attempt_order_retries_and_probes[failed] ________
tests/test_leg_snapshot_replay.py:94: in test_recorded_snapshot_attempt_order_retries_and_probes
    assert probes == [leg.id for leg in expected], (
E   assert [] == ['agy-gemini-...y-sonnet-5.5']
=========================== short test summary info ============================
FAILED tests/test_leg_snapshot_replay.py::test_recorded_snapshot_attempt_order_retries_and_probes[gemini]
FAILED tests/test_leg_snapshot_replay.py::test_recorded_snapshot_attempt_order_retries_and_probes[sonnet]
FAILED tests/test_leg_snapshot_replay.py::test_recorded_snapshot_attempt_order_retries_and_probes[failed]
3 failed in 7.11s
```

All three assertion messages reported `chosen=None; probes=[]`; baseline reviews
skip snapshot errors without retrying or probing, including the Opus candidate.
This is not the old, unsupported claim of a byte-identical 19-row replay with
Opus chosen. A missing chosen leg also exercises the safe assertion diagnostic.
The test calls `_route` directly for plan assertions and indirectly through
`spawn`, with real adapter, planner, host admission, prompt and completion code.
Only external provider/discovery, probe and response inputs are injected.

The same replay on the lane, before the final verification run:

```text
...                                                                      [100%]
3 passed in 3.96s
```

The default affected-tests invocation requires `--base`; with `--base origin/main`
its unmapped changelog fragment conservatively selects `tests` (the entire suite).
Once the new artifact is tracked, the unchanged JSON fixture can also select the
whole suite as a shared fixture. The brief forbids the entire suite, so final lane verification uses `--within`
with the snapshot, adapter, liveness/probe, leg-state, seat and dispatch test files.
Every selected file is run with two workers; no whole-suite fallback is executed.

Final lane command (all 25 selected files, including every touched test file):

```bash
python3 tools/affected_tests.py --base origin/main -n 2 --run --within \
  tests/test_leg_adapter.py tests/test_leg_snapshot.py tests/test_leg_snapshot_replay.py \
  tests/test_route_seat_slot_family.py tests/test_leg_probe_w1.py tests/test_leg_probe_d3465.py \
  tests/test_leg_liveness.py tests/test_leg_state_flip.py tests/test_leg_hung.py \
  tests/test_spawner_leg_state.py tests/test_spawner_probe_host_not_ready.py \
  tests/test_spawner_probe_lows.py tests/test_omniroute_state.py tests/test_dispatch_refusal.py \
  tests/test_dispatch_refusal_booking.py tests/test_dispatch_notify_join.py \
  tests/test_registry_route.py tests/test_registry_seats.py tests/test_quota_pause.py \
  tests/test_spawn_cli_probe_text.py tests/test_spawn_mcp.py tests/test_leg_refusals.py \
  tests/test_spawner_limit_fallthrough.py tests/test_leg_served_ids.py tests/test_spawner_paid_last.py
```

```text
======================= 844 passed in 145.94s (0:02:25) ========================
affected wall seconds: 147.627
```
