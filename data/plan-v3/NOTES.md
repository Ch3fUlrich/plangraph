# Plan-v3 import draft — notes, reconciliation and judgement calls

Source: `data/source/plan-v3/` (README phases/gates/ownership, T0–T7 lane tables + their
`Order` sections, STATUS.md) plus `docs/PROPOSAL.md` / `docs/KEEPER.md` for the `plangraph`
track. Checked by `python3 tools/check_import.py` (PyYAML + graphlib, exit 0). Files:
`T0.yaml`…`T7.yaml`, `PG.yaml`, `gates.yaml`; import states are open|claimed|in_review.

## Counts
- by project: autoos=52, server=14, workstation=12, routing=11 (incl. 4 gates + goal), plangraph=4.
- by track: T0=9, T1=13, T2=16, T3=8, T4=7, T5=11, T6=14, T7=6 = **84 lanes**; PG=4; +4 phase
  gates (P0–P3) + goal = 93 items.
- by state: open=84, claimed=2 (T6-SECRETS, T6-IPV6), in_review=7.
- estimates (`estimate_src: default`, 1/2/3/4/6/8 h) chosen by complexity, guided by the
  per-track STATUS effort: T0+T3 ≈5 h/8 h, T1 ≈20 h, T2 ≈24 h, T4 ≈10 h, T5 ≈2–3 d,
  T6 ≈5–8 d, T7 ≈8 h. PG estimates use `estimate_src: plan`: M2A 24 h, M2B 16 h, M3 16 h, M4 4 h.

## Id reconciliation
`grep -o '^| T[0-9]-[A-Z0-9-]*' T*.md | sort -u` = 84 distinct lane-table ids; all 84 got one
item (one per first-column id). `PG-M2A/M2B/M3/M4` are new (routing-00 decision, PROPOSAL M2–M4).
- STATUS-only ids that are **not** lane-table ids, folded into parent notes, not items:
  `T0-BUDGET`, `T0-PAID-2a/2b/2c`, `T1-CREDIT-FIX`, `KEYNAME`.
- `T6-VOICE` (README phase 2, no lane id) maps to **BOTH** T6-HA and T6-PHONE; both are in
  GATE-P2's `depends_on`.
- README phase wildcards expanded: `T0-*`=all T0; `T3-CI*`=T3-CIGREEN+T3-CIMAIN;
  `T1-PROV*`=T1-PROV; `T5-*`=all T5; `T7-*`=all T7.

## How gates connect (judgement call)
`GATE-P0..P3` are `kind: gate`, project routing, track P0–P3; `routing:GOAL` is the single
goal. Per README: phase N+1 lanes `depends_on` GATE-PN; each gate `depends_on` its listed
phase-N lanes; GOAL depends on GATE-P3 and on every leaf lane.
- **T5-CERT exception** (accepted as is): README lists `T5-*` in phase 2, but T5-CERT's text
  depends on phase-3 T4-CERT; kept in **GATE-P3**, depends GATE-P1 + T4-CERT.
- **Leaves into GOAL** (accepted as is): every lane named in a gate or with a dependent
  reaches GOAL through the gates; the 28 leaf keys wired straight into `GOAL.depends_on` are
  unchanged.
- `plangraph:PG-M4` is also wired into `GOAL.depends_on`. No other plan lane depends on PG-*
  except T7-BOARD (`plangraph:PG-M4`).

## Ownership / project calls (accepted)
- T0-HOLD, T0-L1, T4-CERT, T4-SWITCH, T7-GRAPH-R → **routing** (routing-00).
- T0-CONTAINER, T0-KEYS, T0-ZEN1 are OPERATOR lanes on AutoOS components → **autoos**.
- T6-SECRETS, T6-IPV6 are the prox lanes → project **server**, `owner: prox`.
- **T7-BOARD** → project **routing**, `owner: routing-00`; now waits on `plangraph:PG-M4`.
- T5-GRAPH and T5-GROK are now `operator: true` (credential / trial+webhook steps).

## Decisions applied (routing-00)
- ADD deps: T2-RESEARCHCARD←T1-WEBTOOLS; T6-FIX←T6-HEAL; T6-HA←T6-OWUI.
- KEEP OUT as deps, `priority: 1` instead: T2-XMSG, T2-XSECRET (were "after T1-CLEAN").
- KEEP OUT, `priority: 0`: T0-CONTAINER←T4-HARDEN, T5-PROVIDERS←T1-CREDIT,
  T5-HANDOFF←T3-CLEAN, T6-LEDGER←T7-BOARD. T2-CANARY/T2-LEAN positional only; T3 chain as is.
- T6-VOICE → both T6-HA and T6-PHONE, both in GATE-P2's `depends_on`.
- T5-GRAPH `operator: true` + `evidence_kinds` include `decision`; T5-GROK `operator: true`
  with "trial end date + webhook from the operator" in its note.
- T7-BOARD retitled to the PG-M4 adoption question; `depends_on [plangraph:PG-M4]`,
  `evidence_kinds [decision]`, done = "Operator decision recorded as a D-id after the
  plan-graph adoption (PG-M4)."
- New `plangraph` track: PG-M2A→PG-M2B→PG-M3→PG-M4; PG-M4 wired into GOAL.
- Legacy "done" lanes with **no commit sha** import `in_review` with note
  `legacy, needs evidence` (plus the STATUS evidence): T0-SYNC, T0-CONTAINER, T0-HOLD, T0-L1,
  T0-WSCLONE, T5-BRANCHES, T5-CLONE. T6-SECRETS and T6-IPV6 import `claimed` (progress notes).
  Everything else stays `open`.
- T3-SCRUB is reported done but **has a sha** (1738258f): outside the no-sha batch, so it
  stays `open` for the keeper to complete with evidence.
- `tools/check_import.py` now accepts import states open|claimed|in_review (done/cancelled
  rejected), prints counts per state, and raises `TITLE_MAX` to 120 for the 104-char PG-M2A
  title (SPEC target ≤100).

## Evidence kinds
Per DONE text: tests/CI/commit/merge/sha → `commit+ci`; run ids/reviews/scorecards/smoke/
session records → `run-record`; Semaphore/deploy/Server playbook/prox → `semaphore-task`;
OPERATOR approvals/choices → `decision`; combined where several apply. Gates/goal = `[any]`.
