---
name: router-accept-record
description: Use when an L1 sends a seat record or "propose ACCEPTED / merge-ready" for a lane. Checks seat countability and evidence by sampling, then accepts, redirects or grants a waiver.
---

Source `router/config` in the router data checkout and resolve `bin/` under
`ROUTER_HOME`. The gate reads `PG_PGK` and `PG_CI_EVIDENCE` from its
environment when set, or resolves the installed wrappers on PATH.

# Accept a seat record

## 0. Phase-batch review (OPERATOR D-2534, DECISIONS 3.0)
Phase work arrives as ONE GO-REQ per phase branch (phase/<key> vs main), not per task. Check the seat count against the phase tier (highest item tier; HIGH = 2 distinct non-Claude families vs the union of writer families), the CI on the phase head, and that the fix loop sent all findings in one brief. Never ask for per-task seats on phase work. Round 1 = 2 distinct families always (D-2540); after the fix, 1 seat on the delta suffices.

## 1. Tier to seats needed (DECISIONS 3.1)
- normal / test-only / docs: 1 seat.
- HIGH (security, secrets, gateway, deploy, plan keeper, gate files, money guards): 2 seats, distinct families, at least 1 response-side.

## 2. Does each seat count? (all must be true)
1. Seat family is not the writer family and not the other seat's family.
2. Writer family is KNOWN. Unknown or unresolved writer (no run record) counts as anthropic (D-237).
3. Model is resolved or pinned in the record. "assumed-default" alone does not count (D-206).
4. Evidence exists (D-261, D-266, D-270): request-side = record model == CLI model == every DB turn; response-side = gateway call log, sessionTag-attributed, every status-200 row on the pinned served model.
5. Verdict is valid grammar: `pass` or `fail-with-findings`. A PASS line with a BLOCKER inside is invalid and does not count (D-269).
6. Sonnet counts only over a non-anthropic writer (D-239).

## 3. Sample (at least 1 in 3 records; always for HIGH)
```
sha256sum <evidence.json>                       # matches the record?
python3 -c "import json;d=json.load(open('<f>'));print(d['all_match'],d.get('gap_count'),d.get('turn_count'),d.get('expected'))"
git -C <writer-sandbox> rev-parse --short=8 HEAD  # == claimed tip?
git -C <sandbox> diff --stat <base>..<tip>        # scope as claimed?
```
For a delta claim: verify the diff touches only the claimed files.
For an untrusted writer: the added lines must have no URLs or network calls (`git diff -U0 base..tip | grep -E '^\+' | grep -iE 'https?://|socket|requests\.'`).

## 4. Decide
- All good: "ACCEPTED (D-NNN), merge-ready in train order".
- Seat invalid: name the rule, say which seat to rerun on which family.
- Second family unavailable over 1 h: waiver per D-217: 1 resolved seat + 1 Sonnet, own D-id with `Waives: <key>`, plus an extra empirical condition (for example a canary run) for deploy tier (D-299).
- A writer that continued after a tool or hook denial: discard that run (D-271).
- Rework cycles over 2: the L1 must replan, not retry.
- Findings the L1 calls "not real": verify 1 or 2 of them in code yourself; refuted = the seat counts (D-302). Never let the L1 re-run a seat with its rulings in the prompt.

## 5. Log
D-row naming tip sha, seats, evidence shas you sampled.

## GO-REQ plan block (D-4692, D-4926)
For a landing train, copy the inline `GO-KEYS [project:A,project:B]` block from
the head-bound GO-REQ into the GO row before the PLAN block. The keeper treats
that block as the full scope of the GO. Keep its brackets and comma-separated
full keys when printing or copying the GO row.

Put `PLAN <item-key> conforms=yes|no deviations=<text, or none>` last in every GO-REQ and in the GO row's decision cell. The key is the keeper item key; pass it to the gate with `--key KEY`, and the gate also needs `pgk show KEY` to succeed. `bin/go-row-gate` reads the block after GO-HEAD validation and refuses (exit 5) when:
- the block is missing, repeated, or its key differs from the item;
- `conforms=yes` has any deviations value other than exactly `none`;
- `conforms=no` has an empty, punctuation-only or `none`-led value (a first token exactly `none` describes no departure);
- the keeper wrapper is unavailable or times out (10 s): `STOP: keeper wrapper unavailable`, `STOP: no keeper item KEY`, `STOP: keeper timed out KEY`; missing evidence gives `STOP: evidence wrapper unavailable` (exit 6).
Deviations end at the cell end, a pipe, or the next ` word=` or `; word=` field; quoted prose keeps its text, and a quote-wrapped block is still a declaration. `bin/merge-part` shares the gate's helpers (`bin/merge_paths.py`): `PG_PGK` and `PG_CI_EVIDENCE` must be absolute existing files, else the wrappers are found on PATH (absolute entries only, no cwd search, no dated host default). Hosts without them refuse every GO and merge on purpose: install the wrappers before enabling the check. Successful output names the item key, conformance and deviations so the GO record exposes departures.
