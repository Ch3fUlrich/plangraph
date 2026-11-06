# Omnigraph fleet usage — operator decision proposal

Status: proposed; approval authorizes later scoped work, not deployment or writes.
Extends `2026-11-05-omnigraph-memory/plan.md` and `batch.yaml`; does not replace them.
No batch, keeper, skill, service, access grant or graph is changed by this document.

## Evidence and limits

- Read both baseline files in a local data checkout: neither exists in this worktree
  or the inspected `main`. Their item definitions and dependency edges are the baseline.
- Checked `.mcp.json`: pinned `@modernrelay/omnigraph-mcp@0.13.0`, graph `plangraph`,
  runtime environment references for access; this is configuration, not a live probe.
- Checked `deploy/README.md`, `deploy/keeper-proxy/README.md` and
  `skills/mcp-servers-setup/references/omnigraph.md`: install is separately gated;
  the reference describes typed memory, branching and scoped queries, not a feed API.
- Checked `lane/og-mem-contract:docs/og-memory-contract.md` and
  `rust/og-memory/src/lib.rs`: offline v1 validation, four accepted fact kinds,
  schema capability checks, source/actor provenance, Task/routing-prefix refusal,
  redaction-based refusal without rejected input in errors. No transport there.
- Checked `src/plangraph/spawner/{runner,context_pack,launch_spec,seat_spawn,sessions_agents}.py`:
  prompt packing exists; review isolation clears MCP/plugins; managed sessions have
  separately supplied strict MCP configuration. Neither establishes a memory gateway.
  Repository grep found no `WORKER-TOOL-SURFACE` definition; its exact policy is unverified.
- Operator-measured facts, not independently live-verified here (2026-11-06): central
  graph is live; two participating seats read through tools; another awaits scoped
  access under the existing actor lane; writes are central-only; OG-LOCAL-COPY is unbuilt.
  Commits, branches, change feed and `ifGraphCommit` are reported capabilities;
  their exact wire contracts, cursor semantics and retention remain unverified.
- Operator-reported restart context cost: 50–80k tokens per Claude session.
  Keeper already holds plan state. No measured savings or latency is claimed here.
- All recommendations below are future requirements, not claims of implemented behavior.

## Named amendments to the existing plan and batch

1. **U-HOMES** extends CONTRACT/IMPORT: graph is durable why/how, not a keeper mirror.
   Keeper alone owns state; handoff cards are disposable keeper-derived projections.
   Resolve baseline IMPORT's `reference -> Component` mismatch: current v1 rejects
   Component; retain blocked references until an explicit schema/mapping extension.
2. **U-READ** extends IO/LEGS: commit-pinned bounded recall and shared feed invalidation,
   retaining the baseline 8 KiB recall default; no full startup graph or ledger export.
3. **U-WRITE** extends IO/SYNC: batch, dedupe, conditional commit and durable ambiguous
   outcome reconciliation through the existing single facade; no second writer service.
4. **U-TRANSMIT** extends LEGS/E2E: durable fact plus small wake hint, not graph-as-bus.
   LEGS Done retains the baseline negative tool-manifest test (WORKER-TOOL-SURFACE).
5. **U-SAFETY** extends CONTRACT/IO/E2E: quarantine, correction lineage, cache invalidation
   and secret-safe retained-blocked accounting before broad import or activation.
   Add actor/source-filtered recall/audit and dependent-fact blast-radius correction reports.
6. **U-REALITY** updates ACCESS/OP-ACCESS acceptance: re-probe the now-readable seats,
   finish the existing actor access lane; acceptance still requires workstation and
   18-de-2053 authenticated read evidence, not just currently readable seats.
   Do not claim ACCESS complete from configuration or an old baseline inventory.
   Retain explicit OP-ACCESS login approval and custodian authorization, plus
   Q-OMNIGRAPH-WS placement action and retirement review.
7. **U-GATES** preserves IO's mock-first independence from ACCESS and the acyclic
   SYNC -> OP-GO -> E2E activation sequence; adds freeze clearance to live write gates.
   Baseline plan says skill briefs released while batch retains OP-SKILL: reconcile
   the recorded ruling before SKILL-COMPRESS, never infer permission from this proposal.
   Keep ACCESS and live E2E independently reversible; retain HOST-NAMESPACE-MIGRATE and
   server sync coordination gates, with explicit operator decisions before activation.
8. **U-LAYOUT** leaves graph folding/aliases to MEM-OMNIGRAPH-MERGE; retain per-repo
   layout pending that decision. Resolve baseline compression's contradictory ban on
   per-project instructions before editing skill text. No fold, deletion or restore here.
   Remove the obsolete graph-creation fallback: pre-merge plangraph writes target the
   existing graph `plangraph`, not hub `plangraph` in `autoos`; folding stays deferred.

These are proposed acceptance-criteria amendments to the named items, not new parallel
items. Cost: focused Rust facade/launcher tests and operator review; no new persistent LLM.

## 1. Information homes

Recommendation: store reusable, evidenced knowledge once; use references everywhere else.
Cost: relevance/provenance review per new fact and an extra authoritative lookup for state.

| Information type | Home | Why | Who writes | Who reads |
| --- | --- | --- | --- | --- |
| Durable rationale, constraints, conventions, explicit preferences | Graph, typed and scoped | Reusable why/how across restarts; revision lineage | Validated facade on behalf of attributed actor | Scoped recall clients and launcher |
| Tasks, plan status, ownership, dependencies, evidence verdicts, decision ledger | Keeper | Already authoritative; no graph replica | Existing keeper-authorized actors | Planner, workers via bounded keeper context |
| Current attempt, acceptance checks, next action, blockers | Keeper; card is disposable derived view | Keeper wins every disagreement; no second state home | Keeper-authorized actor; attempt owner refreshes projection | Successor and launcher |
| Wake, urgent stop, question, acknowledgment, coordination | Chat/message channel | Addressed delivery and urgency, not durable recall | Participating agents/operator | Named recipient |
| Code, tests, proposal text, implementation history, large evidence | Git/artifact store | Reviewable exact bytes; avoid graph payload growth | Existing repository/artifact writers | Implementers/reviewers; graph stores safe references only |
| Reusable lesson derived from a task or decision | Graph with keeper/git reference | Deliberate small semantic extraction, not copied state | Facade after evidence review | Future relevant sessions |
| Rejected/pending submission accounting | Private host runtime ledger | Recovery, not a second knowledge base; status only in recall | Supervisor that alone may reach central | Submitter/operator; no rejected body exposed |
| Secrets, raw transcripts, unreviewed instructions | Never graph, feed payload or recall | Prevent disclosure and instruction laundering | No memory writer permitted | No recall consumer |

Keeper/card/git reads remain necessary. Do not import their bodies merely to save a lookup.
Cards project keeper state; graph references never override keeper state or evidence verdicts.
Global recall is explicit opt-in; no automatic cross-project fan-out or preference hub edges
that v1 refuses. Durable actor/source metadata must survive transport, not just validation.

## 2. Reading efficiently

Recommendation: launcher requests project + task topics + applicable approved globals,
selected fields, current revisions, source references and graph commit. Filter authorization
before ranking; bound traversal depth, result count, query time and rendered bytes.
Use the baseline 8 KiB output cap, including provenance/status; select whole facts rather
than cutting safety qualifiers. Return omitted-count/continuation and fetch details on demand.
Numeric traversal/time limits require IO profiling and operator acceptance, not invented defaults.

At start, combine this recall artifact with the existing small keeper/card context; do not
append it to an unchanged full reload and call that a saving. Cache only authorized,
validated, commit-keyed recall artifacts; label unavailable memory separately from empty results.
No cache is OG-LOCAL-COPY and no cache is authoritative offline memory.

On change, prefer one host-runtime feed consumer with a durable cursor and scoped invalidation
over per-session full-query polling. Wake only affected sessions; retrieve bounded deltas at
safe task boundaries, not mid-edit. Check feed continuity, permissions and head commit;
on cursor expiry/gap perform a bounded re-recall, never assume no changes.
Until feed behavior is verified, use coalesced head checks at explicit recall/task boundaries;
any timer cadence needs an approved freshness requirement. The baseline five-minute scanner
default concerns source-file discovery, not a justified feed/polling latency target.

Estimate (unmeasured): saving = replaced startup memory tokens minus recall/invalidation tokens;
the 50–80k restart load is only reduced by the portion actually removed, not by its entire size.
8 KiB is bytes, not a token estimate. Measure matched tasks/restarts with and without recall:
tokenizer-count startup, recall, follow-ups and repairs; record wall time, relevance misses,
stale decisions and successful outcomes. Keep rollout off if net context cost or correctness worsens.
Cost: cursor/cache bookkeeping, extra targeted reads and relevance evaluation; benefit is
bounded startup and change traffic rather than repeatedly loading unchanged history.

## 3. Writing efficiently, with state and history

Recommendation: all session/import submissions use OG-MEM-IO's Rust facade; thin MCP/CLI
adapters never implement alternate mutation logic. Preserve the baseline routing-ledger
sync exception restricted to routing-prefixed records; do not pull it into this writer.
One path means shared semantics, not an unprotected global lock across distributed clients.

- Validate scope, kind, schema, source and secret refusal before network. Batch related
  accepted facts at task completion/source scan; urgent safety correction bypasses batching.
  Cap batch size and wait by measured limits; no numeric throughput assumption.
- Dedupe on project/kind/stable slug and content/source hash; repeated body and relationships
  are no-ops. Persist request identity, intended node/edge set and expected graph commit.
- Apply with `ifGraphCommit`; on conflict re-read the affected scope, reconcile nodes AND
  edges, retry unchanged independent facts within a bounded policy. Semantic disagreement
  becomes retained-blocked review, never silent last-writer-wins.
- Success requires commit receipt plus read-back of intended content and edges. Timeout
  after submission is outcome-unknown, not failure-to-write: reconcile request identity,
  hashes, edges and history before retry; do not mint another slug or duplicate edge.
- Preserve old revisions, attribution, reasons and safe evidence references. Current recall
  excludes superseded/retracted facts; history is explicit investigation, not startup context.
   No versioned graph-branch staging: approved writes use canonical graph history through
   the same facade; risky imports remain blocked until safe for that path.
- Retain safe correction lineage and import manifests; checkpoint only verified outcomes.
  Track live fact count, revisions, bytes, duplicate/blocked rate and feed lag. Archive large
  source evidence in git/artifacts; no automatic graph deletion or history compaction until
  operator approves retention and server capabilities. Bound pending queues; reject visibly
  on capacity instead of silently dropping or acknowledging unsaved input.

Cost: metadata/read-back traffic and serialized conflict work; batching lowers commits but
adds waiting. History grows with actual revisions; retention sizing remains unverified.

## 4. Agent-to-agent transmission

Recommendation: a verified graph write replaces repeated explanatory messages only for
durable reusable knowledge. Send a small scope/slug/commit hint through existing messaging
when a specific reader must act; feed invalidation serves subscribers, not delivery receipts.
Reader rechecks authorization and commit, then recalls the fact; acknowledge separately.
Never replace assignment, urgent stop, approval, keeper transition or handoff with a graph write.
No graph mutation alone implies that another agent has read or accepted it.

Latency is batch wait + commit/read-back + wake/feed delay + reader scheduling + recall;
source-file input also pays scanner delay. No measured latency bound exists here.
E2E measures each timestamp and missed/replayed wakes; urgent control stays on messaging.
Cost: one small hint where needed, subscriber/cursor overhead; saves repeated knowledge bodies.

## 5. Exact no-MCP flow (U-READ/U-WRITE/U-TRANSMIT under LEGS)

1. Launcher or supervisor, which alone may reach central, derives project and allowed scope from trusted launch
   metadata, not worker-supplied access claims; calls facade recall before launch.
2. It validates output, caps it, injects a facts artifact with commit, age, sources and
   unavailable/omitted status through existing prompt packing. Memory is quoted data,
   never instructions that can override policy, scope, tools or acceptance checks.
3. A coding worker emits a v1 typed remember envelope into a designated private submission
   artifact, using its allowed file output. A text-only review seat returns a distinct typed
   result field; supervisor extracts it with a strict schema, never parses arbitrary prose
   as a command. These integration channels are proposed, not existing memory interfaces.
4. Supervisor bounds and validates size/schema, binds authenticated run/actor/source context,
   rejects scope escalation, runs the same facade and holds credentials outside the worker.
   No raw graph query, access material, MCP server or credentialed CLI is added to workers.
5. Facade returns verified commit/read-back receipt, blocked, or outcome-unknown; supervisor
   records safe status in the run result and returns it to the seat or its successor card.
   A card only projects keeper-owned state; runtime receipts are evidence, not task transitions.
   Worker exit is not persistence success; reconciliation survives worker disappearance.
6. Runtime wakes interested readers only after verification; next launch gets bounded recall.
   Interactive no-MCP seats use this same mediated request/result flow, not hidden direct IO.

Cost: launcher/result-schema integration and durable supervisor accounting. Existing packing
is reused, not a second prompt-state store; test the final rendered prompt against its cap.

## 6. Failure, poisoning and corrections

Recommendation: fail closed for persistence/trust, visibly degrade optional recall.
Cost: some launches lose optional memory and disputed knowledge waits for human review.

- Central unavailable/auth refusal: recall nonzero with explicit unavailable banner; permit
  work only where memory is optional. Memory-dependent safety decisions block. Remember
  stays unsaved/outcome-unknown in private runtime accounting; no silent local knowledge fork.
- Stale copy: OG-LOCAL-COPY is not built. A future copy may serve explicitly stale historical
  research only with commit/age labels; not current rules, approvals or write reconciliation.
- Poison/wrong memory: allowlisted types/fields and evidence references, scoped authorization,
  source review and quarantine precede publication. Validation alone does not prove truth or
  detect every secret. Never execute retrieved instructions or auto-promote candidate facts.
- Secret refusal: reject before network/log/feed/prompt; payload-free reason only. Keep any
  original source private and untouched, not copied into a manifest. Approval cannot bypass
  secret refusal. Read-side validation/quarantine also protects against legacy graph content.
- Correct wrong entries by a conditional superseding/retraction revision with rationale and
  evidence; preserve safe prior history, invalidate caches, wake affected readers and require
  re-recall before dependent action. Do not restore the whole graph or erase unrelated work.
  If secret material already entered history: block recall/export, contain and rotate via
   operator incident process; ordinary supersession cannot remove historical disclosure.
- Actor/source-filtered recall and audit enumerate all authorized facts, revisions and edges
   attributed to a writer or source, including superseded/quarantined entries, with commit,
   provenance and pagination/completeness status. A partial audit cannot claim containment.
   Produce a secret-safe blast-radius report tracing derived facts, dependent edges and
   affected recall artifacts/readers; quarantine or retract tainted dependents through the
   same conditional facade, invalidate caches and require re-review/re-recall before reuse.
   Keeper owns incident state and correction verdicts; graph retains safe knowledge lineage.
- D-3895 freeze scope/current applicability is unverified. Treat it as blocking mutations,
  deployments, schedules, canaries and corrections on the frozen central service until explicit
  clearance; permitted read-only inspection/mock work may continue. Never route around it
  via another endpoint, branch, replica or privileged actor.

## 7. Small rollout slices, preserving existing dependencies

Each slice stays within the baseline changed-line cap or is split before implementation;
all flags default off. Rollback disables integration, never deletes shared facts or originals.

- **A — CONTRACT safety/mapping (U-HOMES/U-SAFETY).** Depends on OG-MEM-CONTRACT.
  Done: tests reject secrets/scope escalation/Component, quarantine fixtures never reach
  transport or prompts; mapping decision recorded. Replaces ambiguous import mapping only.
- **B — IO bounded protocol (U-READ/U-WRITE).** Depends on CONTRACT, extends OG-MEM-IO.
  Done: mock tests cover cap, scope, conflicts, lost receipts, duplicate edges, correction
   and feed gaps, actor/source enumeration and dependent correction reports (including
   pagination and historical edges); verify API contracts before live use. Replaces blind retry.
- **C — LEGS mediation.** Depends on IO; extends OG-MEM-LEGS, not ACCESS.
   Done: coding and text-only seats round-trip without MCP or access credentials, safe unavailable
   banner and durable receipts tested; poisoned payload cannot alter launch scope/tooling.
   Assert final leg tool manifests contain no Omnigraph/MCP tools (WORKER-TOOL-SURFACE).
  Replaces repeated memory prompt bodies, not keeper/card context or worker tool policy.
- **D — IMPORT/SYNC accounting.** Depends on IO then IMPORT as existing batch requires.
  Done: every source classified verified/duplicate/blocked, Component blocked, restarts and
  outcome-unknown reconciled; schedule proven off. Replaces legacy scanner write paths only
  after comparison; deletes no sources and enables nothing before OP-GO/E2E.
- **E1 — ACCESS doctor.** Retains SPAWNER-HOST-DOCTOR merge/CONTRACT/OP-ACCESS gates.
   Done: redacted endpoint and authenticated graph/hub/version evidence on every approved
   host, including workstation and 18-de-2053; wrong graph, 401/403,
   timeout, missing hub and old-version tests. Rollback: probe flag off, independent of E2E.
   Replaces obsolete inventory, not access-repair/actor lanes; does not authorize live writes.
- **E2 — live E2E.** OP-GO retains ACCESS/IMPORT/SYNC/LEGS; E2E needs SYNC/LEGS/OP-GO,
   freeze clearance and decisions 8–10 below. Done: redacted cross-host/non-Claude canary,
   correction/propagation, outage recovery, no duplicate nodes/edges, per-host no-loss counts,
   pending-not-saved recall and context/latency/growth measurements. Activate scanners only
   here after OP-GO. Rollback: stop schedules, retain facts/evidence; E1 may remain passed.
   Before D/E2 sync writes, HOST-NAMESPACE-MIGRATE supplies stable host IDs; coordinate
   server:OG-DELTA-IMPORT and server:AO-SYNC-PARTIAL-PULL-RESTORE, never a new restore loop.
- **F — compression/layout follow-up.** SKILL-COMPRESS remains after E2E and reconciled
  OP-SKILL ruling; PH-M2C-KG retains its baseline gates. Done: evaluations and baseline
  line/forbidden-instruction checks reflect an explicit layout ruling. Replaces obsolete
  skill instructions only; MEM-OMNIGRAPH-MERGE owns any later folding/aliases separately.

## Operator decisions (defaults)

1. Approve these named amendments for later item updates? **Default: proposal only; no apply.**
2. Knowledge ownership? **Default: keeper alone owns state; cards are disposable projections;
   graph owns why/how, git owns artifact bytes; no activation.**
3. Read budget/transmission? **Default: baseline 8 KiB bounded recall, verified feed first,
   scoped fallback checks; messages still carry urgency/acknowledgment.**
4. Writer/no-MCP integration? **Default: one Rust facade, credentialed mediation and verified
   receipts as design only; routing exception unchanged; workers gain neither MCP nor access.
   Host-runtime credential placement requires decision 8; no activation by default.**
5. Import trust/type gap? **Default: quarantine uncertain content; Component retained blocked;
   no global fan-out or secret override.**
6. Live activation/freeze? **Default: blocked pending E1 ACCESS, decision 8 OP-ACCESS,
   decisions 9–10, explicit OP-GO and freeze clearance; mock-first only;
   no local replica, fold, compaction, deployment or new schedule authorized here.**
7. Retention/layout/skill ruling? **Default: preserve safe history and originals; defer numeric
   retention and graph folding; reconcile existing operator ruling before skill edits.**
8. Place scoped runtime credentials (OP-ACCESS) on every named participating host, including
   workstation and 18-de-2053? Operator approves host login and obtains custodian
   authorization; only operator places credentials, opaque to agents. **Default: none placed;
   mock work only, no live writes.** Existing access is evidence, not placement permission.
   Q-OMNIGRAPH-WS owns the workstation placement/configuration action; no placement by default.
9. Approve OP-GO named-host canary and later scanner activation? Record the host list,
   scoped access evidence, reviewed import manifests and rollback owner after doctor/mock
   tests pass. **Default: no GO, no live writes or activation.**
   Retire workstation sync via Q-OMNIGRAPH-WS after comparison? **Default: defer retirement;
   old device sync stays disabled, no replacement activation.** Record comparison and owner.
10. Accept HOST-NAMESPACE-MIGRATE IDs and server:OG-DELTA-IMPORT /
    server:AO-SYNC-PARTIAL-PULL-RESTORE coordination? Operator records stable-ID evidence,
    server-owner approval and rollback owner before scanner activation. **Default: defer;
    no sync-write or scanner activation.** Neither dependency is replaced by this proposal.
