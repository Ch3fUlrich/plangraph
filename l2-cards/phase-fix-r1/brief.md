# phase/rt-p1-bus FIX ROUND 1 (D-2540/D-2643) · base phase aded8d82
Seats r1: Haiku 5.5 (verdict fix), qwen3.8-flash qodercli (verdict fix). Seat dir build-L1-bus-seats/phase-r1-aded8d82 (SHA256SUMS).
L1-verified: G1-1, G1-2, G1-3 and G1-4 below were checked in code at aded8d82.
Three parallel writers, file-disjoint, each cut from phase aded8d82 as lane/phase-fix-r1-g{1,2,3}.
- Writer order: D-2665, cx in cooldown until 10-26T02:14Z, then mimo > deepseek-direct > agy. mimo has truncated 3/3 times on keeper briefs, so G1 skips mimo.
- Rules: red test first for each HIGH. Run the affected tests, then `gh workflow run tests.yml --ref <lane>` green. Commit early. Writer trailer: model + run id.
- Merge order: G1 first, then G3, then G2 (G2's real-keeper test needs G1). Send PHASE-READY with head + CI run for each.
- Every finding must be either fixed or answered with `WONTFIX <id>: <reason>` in the report. Nothing is dropped silently.

## G1 keeper side
Files:
- keeper/store_messages.py, messages_poll.py, messages_schema.py, api.py, copy_to_pg.py
- the keeper server start-up that sets bus_registry
- tests/test_keeper_messages.py, tests/test_keeper_message_envelope.py
- docs/FLEET-BUS.md "keeper message envelope"

Findings:
- G1-1 HIGH (both seats): signed text mismatch.
  - Client: client.py:BusClient._payload signs the raw text and sends body {"text": text}.
  - Keeper: store_messages.py:post_message signs json.dumps(body).
  - Rule (keep the client): a signed envelope requires body == {"text": <str>} and verifies against that str. Any other body shape with an envelope is refused as validation. Put this in the docs contract.
- G1-2 HIGH (Haiku V1-1): GET messages with the client's fmt=json is refused as "unknown message filter". read_messages must drop fmt (and wait_ms) before filtering.
- G1-3 HIGH (Haiku V1-2): post_message has a recipient allowlist {"keeper","relay","router","worker","l1","a","b"}. That refuses real agent names and ships test fixture names.
  - Fix: accept any `to` that matches the agent-name rule (the same regex the client uses), within the length cap.
  - Fix the tests' fixtures instead of production code.
- G1-4 HIGH (Haiku V4-1): bus_registry is never set on the server, and Registry.run never starts. So every signed post is refused with "no registry configured".
  - Wire it in: when the keeper starts with PLANGRAPH_BUS_INVENTORY=<file>, it builds a Registry and runs Registry.run in a daemon thread with a stop event that server_close sets.
  - Expose a read-only `GET /v1/hosts` (auth) with host, endpoint, key_id, health, last_seen. Never the key path.
  - Unset env = signed posts refused as today (fail closed). Add a test that starts the real server from env.
- G1-5 MED (both seats): enforce BODY_MAX on the server. post_message refuses a serialized body over the contract BODY_MAX with too_large; api returns 413.
- G1-6 MED (Haiku V2-2): an unsigned router/system post can set any `from`.
  - Fix: add a nullable `posted_by` column to the v15 messages DDL, written into the hash chain. v15 is not on main yet, so edit it in place, SQLite + PG + copy_to_pg.
  - Router/system may set `from` only to their own name or a registered host. `posted_by` is always the principal.
- G1-7 MED (Haiku V1-3): long-poll waiters each open a PG connection with no cap.
  - Cap concurrent LISTEN waiters (default 16, env override). Over the cap, use the 250 ms re-read path.
  - Document that PG_DSN must not be a transaction pooler.
- G1-8 MED/LOW (both seats): copy_to_pg._verify_messages_chain uses `except Exception: return []`.
  - Missing table only: catch UndefinedTable / OperationalError "no such table" and return []. Anything else is raised.
- LOW items:
  - V1-6: api post_message with a non-dict body, or a body key "principal"/"registry", returns 400, not 500. Never splat client keys into kwargs.
  - V2-4: record the nonce in message_nonces on the idempotent and conflict paths too. Validate the envelope inside the write lock, or document why the order is safe.
  - V2-5: non-string msg_id returns 400.
  - V2-6: assert in a test that keeper-side verification uses contract canonicalisation (client canonical_post bytes == keeper bytes).
  - V2-7: refuse an envelope whose "kid" differs from the host key id. Do not take it from the envelope.
  - V2-3: when the bearer principal differs from the signed host, refuse unless `from` == signed host.
- Test V7-2: test_jsonl_outbox_stays_metadata_only must fail when no outbox is written. Assert that the metadata line exists, then assert no body text.

## G2 client, CLI, shims
Files: fleet_bus/client.py, tools/fleet-msg, fleet-msg.cmd, fleet-msg.ps1, tests/test_fleet_msg.py.
Findings:
- G2-1 HIGH (Haiku V7-1, qwen V7): the fake keeper in test_fleet_msg.py diverges from production on text, dedupe and nonce handling.
  - Replace it with the REAL keeper (Store + api server on a temp DB, bus_registry set as in G1-4).
  - Send, read, retry with the same msg_id, replay, unknown agent and body cap must all run against it.
  - Keep a tiny fake only for transport faults (503/timeouts).
- G2-2 MED (V6-2): the spool must fsync the tmp file and the directory before and after os.replace. A corrupt entry is moved to spool/corrupt/ and logged, never skipped silently.
- G2-3 MED (V6-3): the spool directory is 0700 and its files 0600 (POSIX).
- G2-4 MED (V6-4): tools/fleet-msg runs python3 -I (or -P with an explicit sys.path), so the cwd cannot shadow modules.
- G2-5 MED (V6-5): fleet-msg.cmd must not re-expand %* through cmd metacharacters.
  - Delegate to the .ps1 or python with safe forwarding, and document the quoting.
  - Add a test that runs the .cmd when on Windows (skip elsewhere with a reason), plus a parse-level test.
- G2-6 MED (V4-2): the health probe sends no bearer token and must check the keeper identity.
  - Option: compare the key_id in /v1/health with the registry key_id, or a signed health reply. Pick one and document it.
  - A 2xx from another service is not healthy.
- G2-7 MED (V4-4): probe peers concurrently with a short per-probe timeout (2 s), so total build_client time is ≤ about 3 s.
- G2-8 MED (V5-1): build_client wires the real WakeHook when wake is configured (config key). Unconfigured logs once. Test that a send to a configured target triggers the sink.
- LOW items:
  - V6-6: compute the envelope ts after drain.
  - V6-7: validate msg_id as uuid4 hex before using it as a file name.
  - V6-8: a corrupt `created` goes to quarantine and does not crash.
  - V6-9: a relative key_file resolves against the config file's directory.
- Test V7-3: shim tests execute the POSIX script for real (already done) and the .cmd/.ps1 where available.

## G3 exec observed state + wake
Files:
- rust/worker/src/{activities,keeper,workflow}.rs
- keeper/store_worker.py
- docs/adr-exec-ownership.md
- tests/test_keeper_exec_*.py
- fleet_bus/wake.py, wake_http.py, tests/test_fleet_bus_wake*.py

Findings:
- G3-1 HIGH (Haiku V3-1): activities.rs reads card_state at call time, so a Temporal retry of a delayed park observes the projector's newer waiting and lands. This is the PR #140 finding, still open.
  - Carry the observation captured when the workflow decided to park/ask (from its last keeper read or the projector event in its history) in the activity input.
  - Never re-read inside the activity.
  - Rust test: a retried park with a stale observation is refused by the keeper path, or its stub equivalent.
- G3-2 MED (V3-2): the cap-park after usage_limit/outage must land even if a keeper read fails.
  - With G3-1 the observation comes from the workflow. Prove it with a test: the cap-park with its recorded observation lands while `GET /v1/show` fails.
- G3-3 MED (V3-3): the ADR says ask keeps the any-of live-set CAS, but the code narrows it. Restore the ADR behaviour for ask (live set), keep observed-only for park, and test it.
- G3-4 MED (V3-4): no blocking ureq call on a tokio worker thread. Any remaining keeper read runs in spawn_blocking with an explicit timeout (≤ 5 s).
- G3-5 LOW (V3-5): cap the JSON body size of card_state.
- G3-6 LOW (V5-3): WakeState.save runs before acknowledging the cursor. On a crash between wake and save, at most one duplicate wake; document and test it.
- G3-7 LOW (V5-4): wake_http does not follow redirects (custom opener without HTTPRedirectHandler), so Authorization is never forwarded.
- G3-8 LOW (V5-2): document the 20 s debounce as "one wake per burst" in docs and test two events 5 s apart → one wake.
- Test V7-4: test_report_wakes_router_within_60s uses the real KeeperMessagesPoll against a real keeper server (temp DB). No self-advanced clock for the bound.
- Test V7-5: a Rust test for the activities park path covers the observation it passes.

## Out of this round (recorded, not dropped)
- V4-3: keeper-persisted registry. G1-4 exposes /v1/hosts from the in-keeper Registry; table persistence is the follow-up.
- V4-5: routing bin/fleet-poll.py BASE_URL and AutoOS DEFAULT_BIND move to discovery. Cross-repo follow-up leaf; L1 sends it to routing.
- V5-1: removing the 2-minute timer (routing#3) and switching router and workstation to wake are host steps after main.
