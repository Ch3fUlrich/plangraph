# FIX r2 AMENDMENT (D-2739, operator; supersedes brief lines where they differ)
1. WRITER: mimo/mimo-v2.6-pro ONLY (fixer >= writer strength, never flash: agy gemini-3.8-flash is OUT for fixers). If mimo-pro is down -> BLOCKED to build-L1-bus, no substitute.
2. Fix the WHOLE defect class, not only the cited line:
   - every 30x/redirect path in wake_http.py;
   - every activity that degrades a failed keeper read into a fallback (observe, card_state users);
   - every stub that is weaker than the real keeper's CAS.
3. Run the FULL affected suites: cargo test for the whole worker crate, pytest for tests/test_fleet_bus_wake*.py + test_fleet_msg.py + test_keeper_messages.py + test_store_worker*.py on SQLite+PG16, and a full pytest vs base. No "nothing else" scoping.
4. Slice <= ~400 changed lines. If the fix needs more, split it into ordered commits/slices R2a (Rust exec) and R2b (wake + docs), each green.
5. Invariant table (each one is a test assertion):
| # | Invariant | Input/state | Expected |
|---|---|---|---|
| I1 | A fresh run that parks records OBSERVE_PATCH and schedules observe before park | new workflow, usage_limit -> waiting | marker in history; park expected_state = observed ["waiting"] |
| I2 | Old history (no marker) replays unchanged | replay of a pre-marker history | observe_card -> None; no new command; no nondeterminism |
| I3 | A failed observe never makes the cap-park permanently refused | observe fails once (timeout/500), card waiting | park lands within bounded retries (re-observe on exec_stale_state) |
| I4 | A live-set fallback is never accepted for park on waiting/needs_input, in the stub or the real keeper | StubKeeper + store_worker, held state waiting | refused exec_stale_state in both |
| I5 | A wake POST is never downgraded | 301/302/303 same-origin | WakeBusy, cursor kept; 307/308 followed as POST with body + bearer |
| I6 | A cross-origin redirect never forwards Authorization and ends | cross-origin 30x, N tries | WakeRefused after N, logged; second origin gets 0 requests |
| I7 | Transport errors stay inside the contract | malformed Location port | WakeBusy/PollFailed, never ValueError escaping |
| I8 | Wake daemon entrypoint runs or fails closed | valid / invalid config | one wake against real keeper + stub session / exit !=0 with message, no wake |
| I9 | No key path or site IP anywhere | scrub_check, bus_hosts rows | 0 hits |
6. The seat-B review follows VERBATIM. Every HIGH/MED/LOW in it is in scope: FIXED with a named test, or WONTFIX <id>: reason. Part-A-owned lines in it (V7-1/2/3, V7-6/7) were judged FIXED/by-design by seat A: mark those N/A-A.
