# R2b-1: wake redirect class (lane/phase-fix-r2b, base = phase/rt-p1-bus after the R2a' merge)
You are the WRITER. cwd = git worktree on branch lane/phase-fix-r2b. Edit files in place, run the checks, ONE commit. Do NOT push.
Edit only: src/plangraph/fleet_bus/wake_http.py (`_SameOriginRedirects`, `_request`, `_origin`, `SyntheticSink`) and tests/test_fleet_bus_wake_http.py. Nothing else. Write your first change (a red test) within 10 minutes; all anchors are below; never print whole files. <= 200 changed lines.

## Findings (seat text verbatim, r2 seat B)
1. MED "`wake_http.py:_SameOriginRedirects` + `SyntheticSink.wake` — a same-origin redirect on the wake silently loses the wake. `redirect_request` defers to `super()` for a same-origin hop, and urllib's `HTTPRedirectHandler` converts a 302/303 POST into a GET with no body. Failing scenario: the session service sits behind a same-origin 302 (gateway rewrite); the synthetic wake is re-issued as `GET /synthetic/final` without `{"text": ..., "resume": true}`; that endpoint answers 204, `SyntheticSink` sees 204 and returns, so the hook marks the row `woken` and advances the cursor while the session was never woken. ... follow 307/308 only, or treat a 30x on a POST wake as `WakeBusy` rather than success."
2. LOW "`wake_http.py:_request` — an unhandled exception type escapes the documented "never raises" contract. `_origin` calls `parts.port`, which raises `ValueError` for a malformed Location (`http://h:99999/steal`) ... Clamp/validate the port or catch `(OSError, ValueError)`."
3. LOW "`wake_http.py:_SameOriginRedirects` — a permanently cross-origin-redirected endpoint is reported as `WakeBusy`, so the obligation is retried every `retry_s` forever with no give-up path."
4. LOW "`tests/test_fleet_bus_wake_http.py:_server` — the hits tuple records only `(path, Authorization)`, never the method. Both redirect tests therefore pass while urllib silently downgrades the redirected wake POST to a body-less GET ... Capture `self.command` and assert the final request is still a POST with the body."

## Fix (whole class: every 30x path in wake_http.py)
- CPython fact: `HTTPRedirectHandler.redirect_request` follows 301/302/303 for POST as a body-less GET and RAISES HTTPError for POST 307/308. So in `_SameOriginRedirects.redirect_request`: if `req.data is not None` (POST): for 307/308 same-origin build `urllib.request.Request(target, data=req.data, headers=<req's headers incl. Authorization, Content-Type>, method="POST")` and return it; for 301/302/303 return None (the 3xx comes back as a status -> `SyntheticSink` raises WakeBusy, cursor kept). GET (the keeper poll) keeps today's same-origin behavior.
- `_origin`: a malformed port (ValueError) must not escape: treat the target as foreign (return None from redirect_request) and make `_request` map any ValueError to OSError, so `SyntheticSink.wake` -> WakeBusy and `KeeperMessagesPoll.poll` -> PollFailed.
- Cross-origin give-up: `SyntheticSink` counts consecutive cross-origin redirect refusals per (kind, session); after `cross_origin_max` (constructor arg, default 3) it raises WakeRefused (message names "cross-origin redirect") instead of WakeBusy and logs once; any other outcome resets the count. To tell a refused cross-origin hop from another 30x, let `_SameOriginRedirects` record it (e.g. set an attribute on the request/opener) and `_request` surface it; keep `_post`'s return shape `(status, body)` for injected transports.
- Tests: `_server` hits record `(self.command, self.path, Authorization, body)`. Update the two redirect tests to the new shape.

## Invariants (each one is a test assertion; exact names)
| # | Invariant | Input/state | Expected | Test |
|---|---|---|---|---|
| I5 | A wake POST is never downgraded | 301/302/303 same-origin | WakeBusy, final server got 0 GETs; 307/308 followed as POST with body + bearer | `test_a_302_on_a_wake_is_busy_never_a_bodyless_get` (parametrize 301/302/303), `test_a_307_or_308_is_followed_as_post_with_body_and_bearer` (parametrize 307/308) |
| I6 | A cross-origin redirect never forwards Authorization and ends | cross-origin 30x, 3 tries | tries 1-2 WakeBusy, try 3 WakeRefused; second origin gets 0 requests | `test_a_permanent_cross_origin_redirect_ends_refused` (+ keep `test_a_cross_origin_redirect_is_refused_and_never_forwards_authorization`) |
| I7 | Transport errors stay inside the contract | Location `http://h:99999/x` | sink WakeBusy, poll PollFailed, never ValueError | `test_a_malformed_redirect_port_is_busy_not_valueerror`, `test_a_malformed_redirect_port_fails_the_poll_cleanly` |

## Checks (foreground; then one commit)
- python3 -m pytest -q -p no:cacheprovider -n 4 tests/test_fleet_bus_wake.py tests/test_fleet_bus_wake_http.py tests/test_fleet_msg.py tests/test_keeper_messages.py -> 0 failed
- python3 -m pytest -q -p no:cacheprovider -n 4 (whole suite) -> 0 failed
- python3 tools/split/scrub_check.py src/plangraph/fleet_bus tests --rules tools/split/manifest.toml --rules tools/split/scrub_ci.toml -> 0 hits
Commit trailers exactly: `Plan-Item: plangraph:RT-P1-BUS-WAKE`, `Writer: mimo/mimo-v2.6-pro <env PLANGRAPH_RUN_ID or plangraph-spawner>`. No other trailers.
Final report (< 1500 tokens): per finding 1-4 FIXED <exact test name>; the pytest tail lines. Then stop.
