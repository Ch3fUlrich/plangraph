# Plan-graph format for L1s (D-296) — v1, 2026-10-02

One YAML file per change, written by a project's L1 and loaded into the keeper by the router (routing-00) through
`tools/apply_batch.py`. The same format serves a first project plan and every later change. Only fields listed here are
validated. Any other field on an item (e.g. `budget`, `intent`, `workstreams`, `acceptance_extra`) is KEPT, folded into the
item's `note` (YAML import today; API/batch creates after KEEPER-BATCH task B3), so write freely but put real acceptance into `done_criteria`.

## How the graph models a plan (until M2c adds question/decision kinds)
| plan concept | graph item | how |
|---|---|---|
| project goal + intent + workstreams | `kind: goal`, id `GOAL` (one per project) | `title` = goal; `done_criteria` = how we know it is reached; `intent:` / `workstreams:` as extra fields; `depends_on` = the last phase gate(s). The router links `<project>:GOAL` into `routing:GOAL`. |
| phase | `kind: gate`, id `PH-<NAME>`, `track: <NAME>` | `depends_on` = every task of the phase; tasks of the NEXT phase depend on this gate. `done_criteria` = phase acceptance; `budget:` / `owner_l2:` as extra fields. |
| task | `kind: lane`, `track: <phase NAME>` | `owner` (an L1/L2/agent or person), `depends_on`, `done_criteria`, `evidence_kinds`, `review_tier`, `estimate_h`, `priority`. |
| parallel vs sequential | edges only | no edge = may run in parallel; `depends_on` = must wait. Never encode order in titles. |
| blocker: operator task, open question, decision | `kind: lane`, `owner: operator`, `operator: true`, `evidence_kinds: [decision]` | title starts `OP:`, `Q:` or `DECIDE:`; the tasks it blocks list it in `depends_on`; it closes on a router D-id. |
| cross-project dependency | `depends_on: [otherproject:ID]` | qualified key; unqualified ids mean the same project. |

## Item fields (create)
`project` (str, required) · `id` (required, unique per project; convention `[A-Z0-9-]+`, not yet enforced) · `title` (≤ 80 chars recommended; keeper cap 200) · `kind` lane|gate|goal ·
`track` · `owner` · `operator` bool · `depends_on` list · `done_criteria` (testable, ≤ 300 chars) · `evidence_kinds` subset of
`commit+ci`, `run-record`, `semaphore-task`, `decision` (all required) · `review_tier` standard|high (default by keyword:
secret/key/security/gateway/omniroute/deploy/keeper/fence → high; only the router may lower) · `estimate_h` number · `priority` int
(higher first) · `note`. New items start `open`. Workers move state only through claim/submit/verdict, never through this file.

## File
```yaml
batch: 2026-10-02-server-plan          # unique name
principal: routing-00                  # the router applies it
source: /home/s/code/<repo>/docs/plan/<file>.md   # the L1's human-readable plan (optional but recommended)
source_sha256: <sha256 of source>      # `sha256sum <source>`
ops:
  - op: create
    item: {project: server, id: GOAL, kind: goal, title: "All server services behind SSO with daily verified backups",
           done_criteria: "SSO on 12/12 apps; restore drill passed", intent: "...", workstreams: [SSO, BACKUP],
           depends_on: [PH-BACKUP]}
  - op: create
    item: {project: server, id: PH-SSO, kind: gate, track: SSO, title: "Phase SSO done",
           done_criteria: "every SSO task done", budget: "16 h, 400k tokens", depends_on: [SSO-OIDC, SSO-REDIS]}
  - op: create
    item: {project: server, id: SSO-REDIS, kind: lane, track: SSO, owner: prox, title: "Redis session store for Authelia",
           done_criteria: "Authelia uses Redis; one announced logout window", evidence_kinds: [semaphore-task], review_tier: high,
           estimate_h: 3, priority: 2, depends_on: [OP-LOGOUT-WINDOW]}
  - op: create
    item: {project: server, id: OP-LOGOUT-WINDOW, kind: lane, track: SSO, owner: operator, operator: true,
           title: "DECIDE: logout window for the Redis switch", evidence_kinds: [decision], done_criteria: "router D-id names the window"}
  - op: create
    item: {project: server, id: SSO-OIDC, kind: lane, track: SSO, owner: server-L1-main, title: "OIDC clients for 12 apps",
           done_criteria: "12 apps log in via OIDC", evidence_kinds: [semaphore-task], depends_on: [SSO-REDIS]}
  - op: create
    item: {project: server, id: PH-BACKUP, kind: gate, track: BACKUP, title: "Phase BACKUP done", depends_on: [PH-SSO, BK-DRILL]}
  - op: create
    item: {project: server, id: BK-DRILL, kind: lane, track: BACKUP, owner: server-L1-main, title: "Restore drill",
           done_criteria: "a restore from last night's backup passes", evidence_kinds: [run-record], depends_on: [PH-SSO]}
  # later changes in a NEW file:
  # - {op: edit, key: server:SSO-OIDC, fields: {depends_on: [SSO-REDIS, OP-LOGOUT-WINDOW], priority: 3}}
  # - {op: cancel, key: server:BK-DRILL, reason: "replaced by server:BK-DRILL2"}   # corrections = new item + cancel/link, never edit DONE
```
Order: `create` an item before any op that references it. Other ops (router only): `edit`, `cancel`, `legacy_state`,
`add_evidence`, `close_on_decision`, `priority`, `rehome`, `set_kind`, `add_dep`, `remove_dep` (see docs/BATCH.md).

## Rules the validator enforces (the file is refused otherwise)
No cycles · every non-terminal item reaches a goal · no dependency on a cancelled item · DONE items are locked (edit/cancel refused) ·
unknown `kind`/`review_tier`/evidence kind refused · keys unique.

## Process
1. The L1 writes the file in its own repo (e.g. `docs/plan/graph/<date>-<name>.yaml`) and sends the path to routing-00.
2. plangraph-L1 validates it symbolically against the live graph and returns the diff + the new ready set.
3. After routing-00's OK, the router applies it: `python3 tools/apply_batch.py FILE --url … --token-file …` (dry run first).
