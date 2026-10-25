# card: bus-discovery-adopt (L2) 2026-10-25
writers mimo-v2.6-pro (agy gemini skipped 429 ~36m; opencode free skipped: key expired).
AutoOS lane: fix1 applied bcbb88ce (43 tests). seats r2 running: qwen 55ef1f haiku bd825c (review_base, tools-on; prompt-file route failed: sandbox cannot read outside)
routing lane: 52755f21. seats r2 running: qwen 44024c haiku 2c1903
OPEN: routing history docs hold site 100.x in append-only logs -> ask build-L1-bus
next: prepush both, MERGE-READY
r2 seats: autoos qwen APPROVE, haiku REQUEST_CHANGES; routing qwen ACCEPT, haiku REQUEST_CHANGES (redirect token leak). fix2 autoos 6cbba9, fix1 routing 0f18bf running. then r3 seats (same pair), prepush, MERGE-READY
r3: fix2 autoos b54cfb8b (52 tests), routing f604554e (30 tests). seats r3: autoos qwen 183cf4 haiku b6c7d3; routing qwen 5fed6b haiku d46a92
r3 seats: qwen APPROVE both repos; haiku REQUEST_CHANGES both (stamp-mismatch noise + real: ts overflow, neg content-length, proxy; routing poll must be unsigned, HTTPException). fix3 autoos 6c1cbd, fix2 routing e00afd running. then r4 seats, prepush, MERGE-READY
r4: autoos cde78d71 (56 tests), routing 72e613b8 (36). seats r4: autoos qwen 043500 haiku 15a455; routing qwen d31703 haiku 025c9d. then prepush, MERGE-READY
r4 seats: autoos qwen ACCEPT, haiku RC (NUL ValueError); routing qwen RC (decode, redirect on bus leg, scheme, 403), haiku APPROVE. fix4 autoos 965005, fix3 routing d05eff running. then r5 seats (only the dissenting seat + the other), prepush, MERGE-READY
r5: autoos 6d16e43e (58 tests, merged main 282abaab), routing f2fcd67d (170 tests, merged main). seats r5 running autoos 2af55a/4b08b1 routing 97fda8/0fbc47. next prepush autoos, MERGE-READY
r5 seats: autoos qwen RC (scheme-less keeper URL HIGH, refusal log cap), haiku RC (surrogate); routing qwen ACCEPT, haiku RC (wrong-shape JSON). lintfix applied. fix5 autoos 0a59b4, fix4 routing 794ee3 running; then r6 seats (last), prepush, MERGE-READY
r6: autoos HEAD a6f806ff, routing f607d3d1 (190 tests). seats r6: autoos 6f7929/eb955c routing 10eeda/a876c0. prepush autoos running
r6: routing seats qwen APPROVE + haiku ACCEPT at f607d3d1 (done). autoos qwen APPROVE, haiku RC (refusal log OSError) -> fix6 9f541c running; autoos prepush green at a6f806ff (pre-fix6, redo)
r7: autoos HEAD 922025f2 prepush green; seats r7 37699a(qwen)/16b106(haiku)
