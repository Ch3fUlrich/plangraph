# PROPOSAL.md (plan-keeper, option C) — two-seat cross-family review, 2026-10-06 (operator-ordered)

First plan review where both seats' model families are resolved at spawn and fenced against the
writer family (`--not-family anthropic` = the Sonnet v3 edit; D-206 + FAMILYFENCE-4). Spawned from
this session (ZCode/GLM, operator-ordered) via `tools/autoos-agent.py run --tier N --clean
--isolate --lean --read-only --not-family anthropic` from plangraph HEAD 2d9d0b5; sandboxes
one-commit materialisations, read-only (an edit would exit 11), seat A deep lens (architecture +
security), seat B second lens (implementability + consistency).

| Seat | Run id | Agent / combo (witnessed in transcript) | Registry legs → family | Findings | Verdict |
|---|---|---|---|---|---|
| A (t2) | `20261006-102059-plan-review-seata-t2-1b0720` | `t2-worker · t2-worker-clean` | gpt-oss-120b → `openai-oss` | 39 | **NOT READY** |
| B (t3) | `20261006-102100-plan-review-seatb-t3-ad15c5` | `t3-reviewer · t3-driver-clean` | mistral/qwen → `mistral`/`qwen` | 24 | **NOT READY** |

Provenance honesty: families are combo→registry legs + the opencode agent header, **not** the
gateway call-log witness FAMILYFENCE-b wants (no writer run id exists — the v3 edit was written by
hand), so the CLI printed `CROSS-FAMILY: unknown`; the fence still held (anthropic removed from the
whole plan). Seat A exit 10 (no REPORT artifact — worker stopped after the verdict); its full
39-finding report IS in the run transcript, archived below verbatim; harness says relaunch, we
archive instead (content complete, no re-spend). No overlap with §8's seats (both were
`unresolved/unresolved`): this record closes the D-206 gap §8 flagged to routing-00.

## Overlap — independent agreement (highest confidence)
- §6 estimates are false: both seats **measured** the in-tree M2a keeper — 47 modules / 8,238 py
  lines vs §6's "1,000–1,500 lines"; CONSTRAINTS expects >1,500; M2a/M2b/M2c lumped into "M2 ≈ 2–3 d" (A36–38, B8–9).
- Evidence kinds incomplete vs pinned M1-3 / SPEC: `semaphore-task`, `decision`, `[any]` missing (A3, B5).
- Review tiering (D-228 `review_tier`, two distinct reviewers on `high`) absent from state machine
  + loop (A9, B6); no router-only family-waiver verb → unresolved families deadlock DONE (A8, B13).
- Run-record resolution is trust-on-read; needs spawner attestation / authenticated API (A10, B11).
- coding.vm pilot voids "agents have no OS access" (root-equivalent per M1-1): label pilot
  detection-only, require off-host keeper (A1 BLOCKER, B20).
- Snapshot signing impossible stdlib-only → name a dependency or declare HMAC design (B2 BLOCKER; A15/16).
- Reviewer token "TTL ≤ lease" unsatisfiable (lease frozen in `in_review`) → TTL = 4 h review deadline (A20, B7).

## Unique, load-bearing
- A11 (BLOCKER): L1 spawns the reviewer and hands it the token by env — a compromised L1 can cast a
  genuine cross-family verdict; residual understated. Fix: reviewer fetches its own token after
  spawner attestation.
- A12/13/14/17 (BLOCKER+): anchoring only the head hash misses a recomputed longer chain; git tags
  are agent-writable; "head behind anchor" is one-directional. Fix: anchor `(head, seq, prev_head)`
  to an append-only notary; verify chain inclusion, not hash order; state residual as
  accepted-undetected until then.
- A21/22/24: writer token revoked on submit → after `--changes` the loop cannot heartbeat/re-submit;
  same epoch lets stale evidence pass; frozen-lease disposition undefined. Fix: re-issue writer token
  per epoch, round-scoped evidence, renew lease on changes.
- B1/B4 (BLOCKER): §3 says the worker claims, §4 shows L1 claiming/heartbeating; token+epoch handoff
  to the worker run undefined. Fix: pick L1-claims-then-delegates (as `delegate` already does) and
  deliver token+epoch to run W by env/fd.
- B3: §4 issues tokens before the run is spawned; keeper "confirm the run exists" needs spawn-first.
- B10: `commit+ci` names no repo registry or CI client/credentials; B16: pilot imports only the 84
  T-lanes, not PG-4/GATE-4/GOAL-1 (93 unique ids) so multi-project is unexercised; B17: test 5 is
  self-referential.

## Seat A findings (t2-worker-clean, verbatim one-liners)
1. BLOCKER §3/§6 — 0700 `plankeeper` void on the only host §6 names (agents root-equivalent on coding.vm) — label pilot detection-only, require off-host LXC before any P claim.
2. MAJOR §3 — triggers key off "EXISTS(done event)" but no `done` event is named and the predicate cannot lock `cancelled` — name the terminal events and add state to the predicate.
3. MAJOR §3 — only `commit+ci`/`run-record` specified; `semaphore-task`/`decision`/`[any]` missing — enumerate kinds + verifier each.
4. MAJOR §3/§4 — "cancelling can never freeze the system" contradicts `depends_on_cancelled` being a validator error — define the router re-point remedy.
5. MAJOR §3 — "System: ready" contradicts ready being computed, never stored — drop it.
6. MAJOR §2/§6 — "no new infra" hides the off-host keeper host + notary — count them.
7. MAJOR §2/§6 — token cost must be MEASURED; test 6 is a byte sketch — run one tokenizer for real.
8. MAJOR §3/§4 — fail-closed family separation with no waiver path blocks every DONE — add router-only `waive_second_family` (D-217/D-228).
9. MINOR §3/§4 — `review_tier` (D-228) nowhere in §3/§4 — add to definitions + verdict gate.
10. MAJOR §3 — run record not spawner-attested → writer can self-assert family (the D-206 failure) — consume a spawner-signed record or authenticated API.
11. BLOCKER §3 — L1 delivers the reviewer's token by env → compromised L1 can cast a cross-family verdict — reviewer fetches its own token post-attestation.
12. BLOCKER §3 — head-hash anchor misses a recomputed longer chain; no consecutive-anchor check — anchor `(head, seq, prev_head)`, verify chain inclusion.
13. MAJOR §3 — keeper cannot append to a root-owned log; git tags are repo-user-writable — external append-only notary, not git tags.
14. MAJOR §3 — "head behind anchor" catches only non-extending rollbacks — compare by chain inclusion/sequence.
15. MAJOR §3 — no snapshot key distribution/rotation; old signed snapshot replayable — pin key out-of-band + monotonic sequence.
16. MAJOR §3 — token-signed outbox replay is replayable; cross-restart dedup unspecified — per-run HMAC over (item, epoch, nonce, expiry) + persisted nonces.
17. MAJOR §3 — keeper/root rewrite is claimed detected but the anchor cannot detect it — say accepted-undetected or move the notary off-host.
18. MINOR §3 — worker-pushable `commit on main` evidence is not writer-independent — restrict push or name the reviewer as the sole control.
19. MINOR §3 — a cancelled correction erases "corrected by" — freeze `corrects` links.
20. MAJOR §4 — reviewer-token TTL ≤ frozen lease is born invalid — TTL = review deadline.
21. MAJOR §4 — writer token revoked on submit → `--changes` loop cannot heartbeat/resubmit — re-issue per epoch.
22. MAJOR §4 — same epoch on `--changes` lets unchanged evidence re-pass — round-scoped evidence.
23. MAJOR §4 — overdue `in_review` only alerts; nothing transitions out → wedged — timeout transition or reviewer reassignment.
24. MAJOR §4 — frozen lease on `--changes` undefined — renew to now+lease.
25. MAJOR §3 — whole-graph validation on every write = one bad item blocks all projects — quarantine / scoped validation.
26. MAJOR §3 — validator omits `depends_on_cancelled`, `corrects_not_done`, `self_dep`, `dup_key` — enumerate + gate.
27. MINOR §4 — no grace window before reap; epoch fence should carry it — add margin.
28. MINOR §4 — release authority unnamed — name claimant/router.
29. MAJOR §6 — no forged/tampered/stale snapshot test — add.
30. MAJOR §6 — no hash-chain tamper / rollback-refusal test — add.
31. MAJOR §6 — no fail-closed verdict-refusal matrix (family, principal) — add.
32. MAJOR §6 — test 1b passes despite docker/lxd root-equivalence; never runs on the off-host keeper — check groups, run on production host.
33. MAJOR §6 — no token TTL/revocation reuse test, no `--changes`-twice test, no review-deadline reaper test — add.
34. MINOR §6 — no evidence-kind non-substitutability test — add.
35. MINOR §6 — import can fabricate `done` — assert refusal.
36. MAJOR §6 — line/day estimates far below built M2a — re-estimate from measurement, split milestones.
37. MAJOR §6 — estimate omits off-host LXC, notary, paid legs — add.
38. MINOR §6 — token estimate has no measured basis — instrument M2a, re-baseline M3.
39. MINOR §6 — layout/MCP-SDK licences deferred with no fallback — resolve before committing the stack.

## Seat B findings (t3-driver-clean, verbatim one-liners)
1. BLOCKER §3/§4 — L1 "claims" vs worker "claims" self-contradiction; §4 shows L1 claiming/heartbeating — pick L1-claims-then-delegates (code already does via `delegate`).
2. BLOCKER §3/§6 — stdlib-only (§6) cannot produce the public-key signature §3 needs — name `cryptography`/Ed25519 or declare HMAC-in-stdlib.
3. MAJOR §4 — tokens issued before W/V exist; keeper must confirm the run first — spawn, then request.
4. MAJOR §4 — token+epoch handoff to run W undefined — deliver by env/fd to W, W heartbeats with it.
5. MAJOR §3/§6 — evidence kinds incomplete vs M1-3 (`semaphore-task`, `decision`, tier `none`, legacy kinds) — add + test row per kind.
6. MAJOR §3/§4 — `review_tier` (D-228) absent from state machine/verdict/loop — encode it.
7. MAJOR §3 — reviewer token TTL vs frozen lease/4 h deadline contradiction — TTL = review deadline.
8. MAJOR §6 — 1,000–1,500 lines contradicts module list, §3 mechanisms, and the pinned >1,500 — recompute per module, name each mechanism's owner module.
9. MAJOR §6 — milestones lump M2a/M2b/M2c — split with separate estimates.
10. MAJOR §3/§6 — `commit on main` + CI needs a repo registry and a CI API client with credentials — name them.
11. MAJOR §3 — the autoos run-record interface (path/format/permissions) is undefined though family resolution + `run-record` evidence depend on it — specify it.
12. MAJOR §6 — no ownership/tier tests (L1 cannot set DONE; worker cannot edit definitions; tier-high two-reviewer; decision/semaphore evidence) — add one per rule.
13. MAJOR §6 — reviewer separation not directly tested (same-family, principal==L1==worker, unresolved) — add a refusal matrix.
14. MINOR §4/§3 — role vocabulary drift (`--role writer` vs worker; §4 step 7 checks only family, not principal ≠ L1 ≠ worker) — one vocabulary, full predicate in the loop.
15. MINOR §4 — loop omits `pg cancel` — add it.
16. MINOR §6 — pilot denominator excludes PG-4/GATE-4/GOAL-1 (93 unique ids, 84 T-lanes measured in `data/plan-v3`) — import the full graph.
17. MINOR §6 — import→export→diff is self-referential — assert expected counts + sampled edges.
18. MINOR §6 — token-cost "format sketch" vs required measurement — real tokenizer.
19. MINOR §3 — "2×" changes threshold: counter undefined — name and store it.
20. MINOR §3/§6 — pilot on coding.vm contradicts OS-isolation claim (M1-1 root-equivalence) — mark isolation not effective in the pilot.
21. MINOR §3 — undefined terms: principal, family, L2 writer, goal node, "tagged in git" — glossary.
22. MINOR §1/§8 — title says v2 while §8 says v3 — retitle.
23. MINOR §5/§6 — dagre-vs-ELK "day-1" although `vendor/` already ships cytoscape-dagre; page's snapshot verification unspecified — pin dagre, state the behaviour.
24. MINOR §3/§6 — hourly anchor/nightly re-check assume an unnamed scheduler and root-owned dirs — name cron/systemd in the dependency list.

## Reconciliation → PROPOSAL v4 change list (for routing-00's re-review)
1. Host honesty: pilot = detection-only on coding.vm; P-claims gated on the off-host keeper LXC (M1-1) — §2/§3/§6.
2. Decide signing: HMAC-in-stdlib (chain binds under a per-run HMAC; snapshot = HMAC + monotonic seq) or name `cryptography`/Ed25519 in §6 — B2/A15.
3. Redesign the anchor: `(head, seq, prev_head)` to an append-only notary (root cron), chain-inclusion check at start, residual restated — A12/13/14/17.
4. Fix the loop: L1-claims-then-delegates; spawn-before-token; token+epoch to W by env/fd; reviewer self-fetches its token; writer token re-issued per epoch; round-scoped evidence; lease renewed on `--changes`; TTLs from the review deadline — B1/3/4, A20–24.
5. Add `review_tier` + router-only `waive_second_family` to definitions, verdict gate, loop — A8/9, B6.
6. Enumerate all evidence kinds with a verifier + refusal test each; enumerate the validator rules (`depends_on_cancelled`, `corrects_not_done`, `self_dep`, `dup_key`) and scope validation — A3/26, B5/10, A25.
7. Re-estimate from the measured 8,238 lines / 47 modules: per-module line counts, M2a/M2b/M2c split, off-host host + notary + paid legs in the budget — A36–37, B8–9.
8. Test plan: verdict-refusal matrix, snapshot forgery/replay, chain tamper, token TTL/revocation, evidence non-substitutability, import-fabricates-done refusal, full-graph import (93 ids) — A29–35, B12/13/16/17.
9. Vocabulary + glossary pass; retitle v3; pin dagre (already vendored); name the CI/repo registry and run-record interface — B10/11/14/21/22/23.
