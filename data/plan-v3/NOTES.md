# Plan-v3 import draft — notes, reconciliation and judgement calls

Source: `data/source/plan-v3/` (README phases/gates/ownership, T0–T7 lane tables + their
`Order` sections, STATUS.md). Checked by `python3 tools/check_import.py` (PyYAML + graphlib,
exit 0). Files: `T0.yaml`…`T7.yaml`, `gates.yaml`; every item `state: open`.

## Counts
- by project: autoos=52, server=14, workstation=12, routing=11 (incl. 4 gates + goal).
- by track: T0=9, T1=13, T2=16, T3=8, T4=7, T5=11, T6=14, T7=6 = **84 lanes**; +4 phase
  gates (P0–P3) + goal = 89 items.
- estimates (`estimate_src: default`, 1/2/3/4/6/8 h) chosen by complexity, guided by the
  per-track STATUS effort: T0+T3 ≈5 h/8 h, T1 ≈20 h, T2 ≈24 h, T4 ≈10 h, T5 ≈2–3 d,
  T6 ≈5–8 d, T7 ≈8 h.

## Id reconciliation (84 ids)
`grep -o '^| T[0-9]-[A-Z0-9-]*' T*.md | sort -u` = 84 distinct lane-table ids; all 84 got
one item (one per first-column id). Nothing was left out.
- STATUS-only ids that are **not** lane-table ids, folded into parent notes, not items:
  `T0-BUDGET`, `T0-PAID-2a/2b/2c`, `T1-CREDIT-FIX`, `KEYNAME`.
- `T6-VOICE` appears in README phase 2 but has no lane-table id; mapped to **T6-HA**
  (the Home Assistant voice lane) in GATE-P2 and in T6-HA's note. Ambiguous.
- README phase wildcards expanded: `T0-*`=all T0; `T3-CI*`=T3-CIGREEN+T3-CIMAIN;
  `T1-PROV*`=T1-PROV; `T5-*`=all T5; `T7-*`=all T7.

## How gates connect (judgement call)
`GATE-P0..P3` are `kind: gate`, project routing, track P0–P3; `routing:GOAL` is the single
goal ("Plan v3 complete"). Per README: phase N+1 lanes `depends_on` GATE-PN; each gate
`depends_on` its listed phase-N lanes; GOAL depends on GATE-P3.
- **T5-CERT exception**: README lists `T5-*` in phase 2, but T5-CERT's text ("Run the
  opencode certification (T4-CERT)") depends on phase-3 T4-CERT. Keeping both gate edges
  formed a cycle, so T5-CERT is removed from GATE-P2 and moved into **GATE-P3**; it still
  depends on GATE-P1 and on T4-CERT.
- **Leaves into GOAL**: every lane named in a phase gate and every lane that another item
  depends on already reaches GOAL through the gates. The 28 lanes with no dependent and not
  in any gate are wired directly into `GOAL.depends_on` (all listed in gates.yaml) so the
  whole graph is a DAG that reaches `routing:GOAL`.

## Ownership / project calls
- T0-HOLD, T0-L1, T4-CERT, T4-SWITCH, T7-GRAPH-R → **routing** (owner routing-00; CERT/
  SWITCH are routing-00's certification runs/drills).
- T0-CONTAINER, T0-KEYS, T0-ZEN1 are OPERATOR lanes on AutoOS components → **autoos**,
  `operator: true`.
- T6-SECRETS and T6-IPV6 are the prox lanes (STATUS "T6-SECRETS / T6-IPV6 (prox)") →
  project **server**, `owner: prox`.
- **T7-BOARD** has no L1 named (owner "L2 researcher + 2 reviewers"); assigned **routing**
  (routing-00 batches operator taste gates). Ambiguous — least confident.
- T5-GRAPH and T5-GROK need operator actions but are not marked **OPERATOR**, so
  `operator: false` (judgement call; noted on each item).

## Ambiguous deps left OUT (text insufficient; a human should decide)
- T0-CONTAINER ← T4-HARDEN: full DONE (pids/memory) needs T4-HARDEN's compose edits, but the
  lane explicitly runs "now with init only and once more after it lands" → not a hard dep.
- T2-XMSG, T2-XSECRET ← T1-CLEAN: STATUS says "priority after", not blocked.
- T2-RESEARCHCARD ← T1-WEBTOOLS (both need the SearXNG/T6-OWUI deploy).
- T5-PROVIDERS ← T1-CREDIT (DONE "documented in T1-CREDIT report").
- T5-HANDOFF ← T3-CLEAN ("older handoff files archived (T3-CLEAN)").
- T6-FIX ← T6-HEAL (T6-UPDATES describes a runtime HEAL→FIX chain, not build order).
- T6-LEDGER ← T7-BOARD ("links to the board (T7-BOARD)").
- T6-HA ← T6-OWUI (uses the OmniRoute STT/TTS combos OWUI serves).
- T2-CANARY and T2-LEAN "fit between" the T2 Order chain → no positional dep.
- T3-PRINCIPLES/CLEAN/SPLIT/WIN are listed after CIMAIN+PREPUSH but not chained to each
  other; each depends on CIMAIN+PREPUSH only.

## Least-sure items (10)
1 T7-BOARD (project routing vs autoos) · 2 T5-CERT (phase 2→3 move) · 3 T0-CONTAINER
(operator project; T4-HARDEN dep unwired) · 4 T2-RESEARCHCARD (T1-WEBTOOLS dep) ·
5 T6-FIX (T6-HEAL dep) · 6 T6-HA (T6-VOICE mapping; T6-OWUI dep) · 7 T6-LEDGER (T7-BOARD
dep) · 8 T5-PROVIDERS (T1-CREDIT dep) · 9 T5-HANDOFF (T3-CLEAN dep) · 10 T2-XMSG/XSECRET
(priority-after T1-CLEAN unwired).

## Evidence kinds
Per DONE text: tests/CI/commit/merge/sha → `commit+ci`; run ids/reviews/scorecards/smoke/
session records → `run-record`; Semaphore/deploy/Server playbook/prox → `semaphore-task`;
OPERATOR approvals/choices → `decision`; combined where several apply. Gates/goal = `[any]`.
