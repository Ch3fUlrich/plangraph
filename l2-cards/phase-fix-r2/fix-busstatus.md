# R2a' fix BUSSTATUS: bus HTTP statuses static, bus rate limit = msg_rate_limited (routing D-2878, Gate-Edit: D-2878)
You are the WRITER. cwd = git worktree on branch lane/phase-fix-r2a (head 91e47fec). Edit files in place, run the checks, commit. Do NOT push.
Write your first file change within 20 minutes (the red test below). Edit only the needed hunks; never print whole files.

## Problem (CI 40250299519, reproduced locally)
src/plangraph/keeper/api.py `_write`, branch `if name == "post_message":` (lines ~375-382, from commit 503c70df) mutates the shared dict
`api_routes.STATUS` at runtime: `_STATUS.setdefault("bad_signature", 401)`, `unknown_kid` 401, `skew` 401, `replayed_nonce` 401,
`replay_window_full` 503, `rate_limited` 429, `too_large` 413. After any post_message in a process, main's contract test
tests/test_keeper_oversight_scope.py::test_applied_rate_limit_is_forbidden_rate_limited fails on `assert "rate_limited" not in api_routes.STATUS`
(oversight R3: its rate limit stays 403 `forbidden`, no `rate_limited` enum). Repro:
`python3 -m pytest -q -p no:randomly tests/test_keeper_message_envelope.py tests/test_keeper_oversight_scope.py -k 'rate_limit or post or envelope or replay'` -> 1 failed.

## Required change (exactly this; nothing else)
1. src/plangraph/keeper/api_routes.py: register the bus codes STATICALLY in `STATUS` (module level): bad_signature 401, unknown_kid 401, skew 401,
   replayed_nonce 401, replay_window_full 503, too_large 413, msg_rate_limited 429. Never `rate_limited`.
2. src/plangraph/keeper/api.py: delete every `_STATUS.setdefault(...)` line in `_write`; no other runtime write to STATUS anywhere (grep `STATUS[`, `STATUS.update`, `setdefault`).
3. Rename the bus message rate-limit code `rate_limited` -> `msg_rate_limited` (429, same retry_after_s semantics) everywhere the BUS uses it:
   src/plangraph/fleet_bus/contract.py (ERROR_STATUS key + the `BusError("rate_limited", ...)` raise), src/plangraph/fleet_bus/client.py `RETRYABLE_CODES`,
   tests/fleet_bus/test_contract_unit.py, tests/fleet_bus/test_contract_vectors.py, tests/fleet_bus/vectors/{errors,rate,read,replay_key,replay_window,validate_order}.json,
   docs/FLEET-BUS.md (the Errors paragraph and the code table row). Find them with `git grep -n rate_limited -- src/plangraph/fleet_bus tests/fleet_bus docs/FLEET-BUS.md src/plangraph/keeper/api.py src/plangraph/keeper/api_routes.py`.
   Do NOT touch the oversight code (oversight_policy.py, store_oversight.py "rate_limited" reason), governor `rate_limited.*`, rust/, or tests/test_keeper_oversight_scope.py.
4. Red test first, in tests/test_keeper_message_envelope.py, named exactly
   `test_oversight_r3_holds_after_a_bus_post_message_in_the_same_process`: make one bus post_message through the keeper HTTP API (reuse that file's fixtures),
   then assert `"rate_limited" not in api_routes.STATUS`, `api_routes.STATUS["msg_rate_limited"] == 429`, and that STATUS is identical before and after the post
   (snapshot `dict(api_routes.STATUS)`). Also add `test_bus_statuses_are_static` asserting the seven bus codes are in STATUS at import.
   Run it red on the current code before step 1.

## Checks (foreground; then one commit)
- `python3 -m pytest -q -p no:cacheprovider -p no:randomly tests/test_keeper_message_envelope.py tests/test_keeper_oversight_scope.py` (pinned order: bus first) -> 0 failed
- `python3 -m pytest -q -p no:cacheprovider tests/fleet_bus tests/test_keeper_*.py` -> 0 failed
- `python3 -m pytest -q -p no:cacheprovider -n 4` (whole suite) -> 0 failed
- `git grep -n '"rate_limited"' -- src/plangraph/fleet_bus src/plangraph/keeper/api.py src/plangraph/keeper/api_routes.py tests/fleet_bus docs/FLEET-BUS.md` -> no hits
Commit trailers exactly: `Plan-Item: plangraph:RT-P1-BUS`, `Gate-Edit: D-2878`, `Writer: mimo/mimo-v2.6-pro <env PLANGRAPH_RUN_ID or plangraph-spawner>`.
Final report: the exact test fn names you added, the files changed, and the check tails. Then stop.
