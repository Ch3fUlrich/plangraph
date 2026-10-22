# build-L2-bus brief (build-L1, 2026-10-22, D-2201)
Parent: build-L1 (coding.vm; reply via the cross-session channel or workst-L1-autoos relay). Load skill unattended-orchestration. Sonnet, effort medium, slim MCP.
Repo: plangraph (PUBLIC: never site strings, seat prompts or private audit text in commits/PRs/comments). Keeper claims held by build-L1: do NOT claim/submit/verdict.
Own git worktree off origin/main (merge origin/main in via merge commit; never rebase/squash existing commits). NO push/merge to main, NO crons, NO live steps.
A permission/classifier denial stops that step: send the verbatim refusal to build-L1, never work around it.
FILES (D-2198): every writer brief names the exact FILES it may touch (paths, new/edit), tests + CHANGELOG included; >3 code files = split writers.
WRITERS (D-2148, D-2046/2047): non-Claude only, via the autoos-agent spawner (D-2038). cx/gpt-6.1-sol first (served on the workstation gateway) > mimo/mimo-v2.6-flash > omniroute/deepseek-direct-flash; record each skip + reason. NEVER Sonnet/Claude writers. Every new commit carries `Writer: <served model> <run id>`; removing a test needs `Test-Removal: D-NNNN`.
SEATS (D-2195/D-2197): non-Claude, family != writer and != each other: opencode free > openrouter free (laguna-s-2.1, north-mini-code; no nemotron) or deepseek-v4-flash while OR free fails > qodercli qwen3.8-flash > agy gemini-3.8-flash > mimo/mimo-v2.6-flash > agy claude-sonnet-5-5-medium (never the non-Claude seat of a HIGH lane). Seat record = served model + provider + family + verdict.
D-2202: mimo only by provider-prefixed id mimo/mimo-v2.6-flash, never a combo; a local 502 "Cannot clone object" = combo bug -> retry with the provider-prefixed id.
Heavy suites: 1 per orchestrator, 2 per host. Before MERGE-READY: pytest + pytest-postgres + scrub + rust (if rust touched) green on the exact head (gh workflow run tests.yml --ref <branch>).
Report to build-L1 ONLY: MERGE-READY <PR> <sha> <seats>, BLOCKED <item> | what | options *default, GO-REQ (HIGH). No acks/ALIVE. Card <= 40 lines; RECYCLE-READY <card> before 230k.

# L2 brief: build-L2-bus   (lane F)
## Lanes, in order:
1. plangraph:RT-P1-BUS-CONTRACT (HIGH): new branch lane/rt-p1-bus-contract off origin/main. Read AutoOS tools/fleet_bus.py (anchors in desc) from a read-only AutoOS checkout on the workstation.
2. plangraph:RT-P1-BUS-KEEPER (keeper Postgres, HIGH) after BUS-CONTRACT is MERGE-READY (build-L1 claims it then). Body text in keeper, never in the metadata jsonl (D-2024).
3. Low prio (1): plangraph:KEEPER-BATCH-PRECONDITION PR #97 (lane/keeper-batch-precondition, head 7c787302): finish per its PR body; keeper engine = HIGH (2 seats + judge GO-REQ).
HIGH lanes: GO-REQ to build-L1 with branch, red-first head (tests only) and red run before the implementation writer starts.

## Keeper item text (read-only copy)
plangraph:RT-P1-BUS-CONTRACT | Fleet-bus wire contract frozen and versioned: envelope, signing, cursors, acks, error codes
state claimed | kind lane | track RETHINK | prio 8 | epoch 1
claimed_by build-L1 | delegate - | lease 2026-10-22T16:38:08Z | reviewer -
deps plangraph:RT-P1-CONTRACT=done
unblocks autoos:OCGO-BUS-CONTRACT-SHARED, plangraph:PH-RT-RUNTIME, plangraph:RT-P1-BUS-DISCOVERY, plangraph:RT-P1-BUS-KEEPER, plangraph:RT-P1-BUS-RUST
desc D-2134, D-2144 S4. One home for the fleet-bus contract: plangraph (design s10: contracts and the spawner substrate move to plangraph; routing is archived; AutoOS installs plangraph at a pinned tag). Module src/plangraph/fleet_bus/contract.py (version string, envelope, canonical HMAC form, scrub_text, caps, parse_ts, validate order, error codes) plus language-neutral JSON test vectors under tests/fleet_bus/vectors/ that the Python reference AND the later Rust binary both run. Today the read path GET /outbox is unsigned and returns message text (AutoOS tools/fleet_bus.py:17-23, 212-219); an evicted or unknown cursor replays the whole ring (164-173); there is no rate limit and no trace field; one shared HMAC secret with a self-declared from cannot attribute a sender. The envelope carries an optional key id (kid) so RT-P1-BUS-DISCOVERY can move to per-host keys without a second wire break.
done Versioned spec + src/plangraph/fleet_bus/contract.py; envelope with optional trace_id/key/run_id/attempt_n/kid and a message id for dedupe; signing input, cursor, ack and error codes, caps; signed reads (no open GET); evicted cursor = 410, never a full replay; 30 msg/min per sender = 429; JSON vectors cover every rule, green on the Python reference; spec linked from AutoOS and routing docs; no site literal; 2 seats of non-Claude families different from the writer (D-2148 order)

plangraph:RT-P1-BUS-KEEPER | Message history lives in the keeper: append-only messages table, hash-chained, queryable by trace
state open | kind lane | track RETHINK | prio 8 | epoch 0
claimed_by - | delegate - | lease - | reviewer -
deps plangraph:RT-P1-BUS-CONTRACT=claimed, plangraph:RT-P1-KEEPER-PG=done
unblocks autoos:OCGO-BUS-MCP, plangraph:PH-RT-RUNTIME, plangraph:RT-P1-BUS-RUST, plangraph:RT-P1-BUS-WAKE, plangraph:UI-MESSAGES-TAB
desc D-2134. Today a message lives in a 1000-record in-memory ring on one host and in a metadata-only jsonl line (no text); after a restart the messages are gone and nothing can query them. The keeper (Postgres) is the system's system of record (rethink design 2), so messages become keeper rows: append-only, hash-chained like the events table, carrying from, to, body, trace_id, key, run_id, attempt_n, ts and the delivery state. The relay is transport, the keeper is truth. Body text is stored in the keeper and stays OUT of the metadata-only jsonl (D-2024 privacy).
done Append-only hash-chained messages table in keeper Postgres; the relay acks a POST only after the keeper row committed; write verb + read verb by trace_id, key, time range; pg_notify on insert is a hint and a keeper long-poll GET waits on it on a direct (unpooled) connection and re-reads after the last seq, so a lost NOTIFY loses nothing; jsonl stays metadata-only; a row links item key and run/attempt; 2 seats of non-Claude families different from the writer (D-2148 order)

plangraph:KEEPER-BATCH-PRECONDITION | apply_ops precondition: a batch names its base head_seq; edits to items changed since the base are refused
state claimed | kind lane | track RETHINK | prio 1 | epoch 2
claimed_by build-L1 | delegate - | lease 2026-10-22T16:37:53Z | reviewer -
deps -
unblocks plangraph:PH-RT-RUNTIME
desc Audit G11 (D-1998), research docs/p1-audit/research/f-keeper-state.md: apply_ops is all-or-nothing but has no expected-version check (store_ops.py:260-285). The unapplied rethink batch rev 4 would silently drop 7 live PH-RT-RUNTIME deps and 4 RT-P1-E2E deps because depends_on edits replace the list. Planner sessions of the pipeline will write batches unattended.
done Batch header base_seq; an edit or cancel whose item changed after base_seq is refused with the item list (no partial apply); depends_on edits may use add/remove forms; the dry-run prints dropped deps; SQLite + Postgres tests; 2 non-Claude seats.

