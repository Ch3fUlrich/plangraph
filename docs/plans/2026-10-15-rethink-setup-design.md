<!-- Imported verbatim (operator order 2026-10-16). Source: private repo Ch3fUlrich/routing, branch rethink-setup @ 6ff782a, path docs/reports/2026-10-15-rethink-setup-design.md.
     Body below the marker is byte-identical; sha256(body) = 1958911c9a9c4b3a301096e0e296a887c75e0bcaeaa5d94cee27a6d550a2309e.
     Contains site strings: the PG-PUBLIC-READY scrub applies before any public flip. Do not edit; corrections go in a new file. -->
<!-- verbatim below -->
# Rethink of the orchestration setup (plangraph + AutoOS + routing) — design (D-1285)

L1-opus-extra, 2026-10-15. APPROVED by the operator (round 10, with the P2 change D25); cross-family reviews (gpt-oss-120b,
mimo-v2.6-pro) folded in, see section 14a.
Inputs: operator brief (`docs/research/rethink-2026-10-15/00-operator-brief.md`), operator answers rounds 1-10 verbatim
(`docs/plan/rethink/operator-rounds.md`), research R1-R5 (repos), W1-W7 (web), E1 (evaluation of the pasted proposal).
The plan items live in the plan graph (D-1277); this file is the design rationale they point to.

## 0. Goal and decisions in force

Goal (operator): many agents from different providers work long-running and unattended on many projects (1-2 first), without
babysitting, at high quality and speed and low cost, with one overview that shows everything.

| # | Decision (operator, 2026-10-15) | Round |
|---|---|---|
| D1 | Milestone 1 = cost cut + progress speed-up by orchestration (not UI first) | 1 |
| D2 | Claude = planner + judge only; execution and most review on free/subscription/trial legs | 1 |
| D3 | Operator time 30-60 min/day attended + phone answers | 1 |
| D4 | 1-2 projects first; significantly more projects later (target scale) | 1, 3 |
| D5 | Fleet slims down during the build; only urgent parts the operator orders in a chat | 2 |
| D6 | First project = the pipeline builds itself | 2 |
| D7 | Spend: subscriptions (mimo; gemini after the Friday reset) + free tiers (ovh low, openrouter, opencode zen free, others); free Jev ids allowed; deepseek only if really needed | 2 |
| D8 | Every unattended task carries a deterministic check; no check = attended path | 2 |
| D9 | Architecture A: state + code; no persistent L0/L1/L2 LLM sessions | 4 |
| D10 | Engine: Temporal, gated by a 1-day validation; cloud.vm if coding.vm RAM is short (via routing -> server agent) | 5 |
| D11 | Runner/worker in Rust now; control plane (keeper, engine, UI) on coding.vm now; workers on the workstation | 3 |
| D12 | Keeper on Postgres (Docker, coding.vm); Python keeper ported now, Rust keeper later | 4, 7 |
| D13 | Worker/spawner + agent skills move to the plangraph repo; infra MCP stacks move to the Server repo | 5, 6 |
| D14 | Decisions = keeper table, operator-only rows; mechanical events are not decisions | 5 |
| D15 | Review: phase-level, risk-scaled | 5 |
| D16 | One plangraph page with tabs + deep links into the Temporal UI; omnigraph viewer as a tab | 6 |
| D17 | Harnesses: opencode + agy + qodercli + OpenHands | 6 |
| D18 | Rules decide; free Jev runs in shadow | 6 |
| D19 | Bootstrap built by a build L1 + Sonnet subagents | 7 |
| D20 | Cancel superseded fleet items; freeze fleet tooling until M1 metrics; md-sync re-armed as stop-gap (D-1297) | 7 |
| D21 | OmniRoute: MCP read-only pilot for planners only; no A2A, Memory, Cloud, Funnel | 8 |
| D22 | Worker on the workstation runs in WSL2 Linux | 8 |
| D23 | M1 targets "ambitious" (section 9) | 8 |
| D24 | UI: SvelteKit now for all pages and tabs (convert the static page) | 8 |
| D25 | Design approved; agy + qodercli adapters move into P2 (M1 dogfood): gemini-3.8-flash and sonnet-5.5 subscription quota is only usable via agy ACP; qwen-3.8-flash via qodercli is free while qoder credits remain and is used before mimo-v2.6-flash (until rate limits) to save mimo credits | 10 |

## 1. Diagnosis (why change)

- Cost sits in persistent coordination contexts, not in workers (R4): 99 % of Claude tokens are cache re-reads at ~352 k
  average context; split L1 58 %, L2 17 %, router 15 %, judge 6 %; 52-78 % of turns are coordination; 7,452 SendMessage in
  5 days; 1,016 D-rows in 14 days, ~60 % mechanical. The weekly limit was hit at ~4.9 days twice (51 h stall 10-13..10-15).
- Workers on free legs already work (R4/R1): 84-87 % non-Claude runs, ~81 % rc 0. The money problem is not L3.
- The fleet builds itself: ~80 % of AutoOS commits since 10-08 are fleet tooling; 76 prose rules with contradictions; weak models
  do not obey prose (D-601); home-grown mechanics failed silently (md-sync 51 h, ctx-watchdog 0 restarts, a watcher 1,519 failed starts).
- The plan graph is a tracker closed by markdown D-rows (662/662 closes, R3); its lease/review machinery is unused; no push.
- The spawner substrate is good but poll-only and single-host (R1). OmniRoute has unused context/cooldown/stickiness features and
  live-vs-git drift (R2). Cross-host messaging is spec only (R1); Claude messaging is held by bypass-mode receivers (W2).

## 2. Target architecture

```
operator (phone / browser, 30-60 min/day)
   |  SvelteKit plangraph page: Plan/DAG | Runs | Inbox | Costs | Knowledge(omnigraph)   (+ deep links to Temporal UI)
   v
keeper (Postgres, coding.vm)  <-- projection of run events --  Temporal server (Postgres DB, coding.vm or cloud.vm)
   ^  plan items, cards, checks, decisions, questions, costs          |  task queues per host/class
   |                                                                    v
planner / expert / judge sessions (Claude, attended, short)     Rust worker (WSL2, workstation; more hosts later)
   writes cards + checks, exits                                   harness adapters: opencode | agy | qodercli | OpenHands
                                                                  worktree per attempt, checks, outcome probe, merge lock
                                                                        |
                                                                  OmniRoute (window-class combos) -> free/sub/trial legs
```

Component responsibilities (one owner each):

| Component | Owns | Does not own |
|---|---|---|
| keeper (plangraph, Postgres) | plan items, DAG edges, cards (with checks, read/write sets, class), decisions (operator rows), questions/answers, attempt projection, costs, budget state, events + hash chain | execution timing, retries |
| Temporal | execution state of phases/cards/attempts: queues, leases, heartbeats, timeouts, retries, durable timers (usage-limit waits, 12 h default auto-accept), signals (answers, cancel) | plan content |
| Rust worker | activities: prepare worktree, run harness, collect output/cost, run checks, outcome probe, rebase + merge under lock, report | deciding what to run next |
| planner session (Opus/Sonnet, attended) | phase design with the operator: contracts, module-batched cards, checks, risk tags; then exits | running work |
| expert session (short-lived) | answering a question for a project from a context pack + omnigraph, then exits | persistent state |
| judge session (Claude, on demand) | milestone verdicts, rule conflicts, HIGH-risk acceptance sampling | routine accepts (code does them) |
| OmniRoute | model failover inside a window-class combo; context floor enforcement | choosing harness or class |
| omnigraph | durable facts and lessons for planners/experts; Knowledge tab | per-run state |

What disappears: persistent router, persistent L1s, interactive L2 sessions, heartbeat crons, SendMessage status relays, recycle
handoff cycles for orchestrators, markdown ledgers (after the decisions cutover), md-sync.

**One owner per fact (review RV1/RV2 HIGH).** Temporal is the only owner of execution state and of every clock: attempt counts,
retries, leases, timeouts, question deadlines, usage-limit waits. The keeper owns plan content and stores execution only as a
projection: one projector writes append-only events keyed by (workflow_id, run_id, activity_id, attempt), states only move forward,
and the projection can be rebuilt from Temporal history. The UI shows the projection watermark and deep-links to Temporal for truth.
A nightly reconciler compares open workflows with keeper states and raises an Inbox alarm on any mismatch. The keeper stores a
question's deadline value; the timer that fires it lives only in Temporal.
**Routing is keeper-side code** (a plangraph module run as a workflow activity whose result the workflow records). The worker only
executes the assigned target (harness, model, window class, leg tier); it never decides what to run next.

## 3. Data model additions (keeper on Postgres)

- `cards` (an item kind or a 1:1 table): outcome, read set, write set (paths/globs, must be disjoint inside a running phase), exports
  (signatures to preserve), forbid paths, `check` (required command, D8), per-subtask smoke checks, held-out check (optional),
  window class (128k / 256k / 1m), risk tags (auth, secrets, money, migration, public-contract, live-step), estimate tokens.
- `attempts` (projected from Temporal): attempt n, harness, model/combo, class, start/end, exit class (ok, check_fail, usage_limit,
  outage, timeout, stall, contract_defect, needs_input, cancelled), tokens in/out, cost, run id, workflow id (deep link), log ref.
- States: open, ready, running, needs_input, waiting (timer/external), failed, done, cancelled, parked.
- `decisions`: operator decisions and judge rulings only (D14), with closes/waives refs; replaces the markdown decision log.
- `questions`/`answers` with default + deadline; answer emits a Temporal signal.
- `budget`: per provider and the Claude weekly meter (burn per day vs 1/7 of the week).
- Contract: HTTP + event + batch contract frozen and covered by a black-box suite before the Rust keeper rewrite (D12).

## 4. Execution flow

1. Planner (attended) writes a phase: contracts/examples in the project repo, cards in the keeper via a batch, each with a check.
2. Readiness gate (code, $0): check present and runnable (dry run), read files exist, write sets disjoint from running cards, card
   estimate <= 50-60 % of the class's usable window, risk tags present. Fail -> back to the planner queue (Inbox), never to a worker.
3. Routing (code): risk tags + estimate + subtask count -> (harness, window class, attempt budget, review level). Free Jev answers the
   same single-axis questions in shadow; both answers + the outcome are logged (D18). Order of **leg tiers** inside a **window class**:
   trial -> free -> subscription -> paid (deepseek last, D7; concrete leg order in section 5). Readiness also checks the estimate
   against the chosen leg's own max request size (measured window), not only the class percentage.
4. Temporal Card workflow -> Attempt activity on the worker: worktree from the phase branch at a recorded base sha; harness run with a
   minimal tool surface; checks; positive confinement (changed files subset of write set; forbid paths untouched); outcome probe before
   any retry; rebase + merge into the phase branch under a per-repo merge lock.
5. Failure classes and budgets (review RV1/RV2 HIGH). Exit classes: ok, check_fail, flaky_check, usage_limit, outage, lease_lost,
   worker_crash, stall, timeout, rebase_conflict, merge_refused, probe_fail, contract_defect, needs_input, cancelled.
   - Leases: activity heartbeat every 30 s, heartbeat timeout 2 min (lease_lost), start-to-close per window class (default 90 min),
     stall = no output and no tool call for 10 min -> kill the process group. Numbers are defaults, tuned from the P1.1 measurements.
   - Retry budget per card: 3 attempts on its leg tier + 1 promotion, then park to the Inbox with a compact packet
     {card, tier, exit class, check tail}. Promotion = the next **leg tier** (trial -> free -> subscription -> paid) inside the same window
     class from a clean worktree; a window-class upgrade happens only when the failure is a context overflow.
   - check_fail retry carries only the card, the current write-set files and the last 40-60 lines of check output. Flaky detector: two
     identical failures with an unchanged diff -> flaky_check -> quarantine the card to the Inbox. probe_fail = backoff retry.
   - usage_limit/outage: first re-route to the next leg in the same tier; wait only when every allowed leg is limited. Wait = durable
     timer to the known reset (provider header or known schedule, e.g. Friday evening for gemini), else exponential backoff capped at 24 h,
     then park + alarm. Wakes are jittered (no thundering herd). Waiting does not consume attempts.
   - worker_crash / lease_lost: Temporal retries the activity on a live worker; the janitor removes the dead attempt's worktree and
     branch after the outcome probe; a zombie worker cannot merge because merge requires the current attempt's fencing token.
   - contract_defect -> planner queue (Inbox). Every exit class is an event in the keeper.
6. Merge path (RV2 HIGH): card -> phase branch merges serialise through a Postgres advisory lock taken via the keeper API (TTL + owner
   heartbeat, stolen only after expiry). Before merging, the worker re-checks positive write-set confinement and, if the phase head
   moved, rebases and re-runs the card's checks; rebase_conflict -> the card re-queues on the new head (counts as an attempt).
   Phase -> main goes through a PR + CI, never a direct push.
7. Phase end: phase review by 1 cross-family reviewer (standard) or 2 (risky; auth/secrets/live = 2 non-Claude seats, rule kept),
   reviewing the phase diff + contracts in full (no truncation; split by module if large). Verdict schema (JSON):
   pass | fix (findings become fix cards in the same phase) | halt (Inbox). A reviewer that times out is replaced by another family.
   The gate command runs as a worker activity. Reviewer questions are Inbox questions like any other.
8. Questions: any session or worker emits a structured question -> attempt ends in needs_input -> Inbox (phone) -> answer = signal ->
   new attempt resumes (session id where the harness supports it). On expiry: a question with a default takes the default after its
   window (12 h policy) and records `question_timeout`; **risk-tagged questions (auth, secrets, money, live step, public release)
   have no default** and stay parked until the operator or judge answers.

## 5. Sizing and model routing

- Cut cards on module boundaries, not by size; batch cohesive subtasks that share read/write sets (fewer re-ingests).
- Window classes as OmniRoute combos: `*-128k`, `*-256k`, `*-1m`, each with `contextRequirements {minContextWindow, strict}`,
  providerCooldown on, targetTimeoutMs set, sticky session per attempt (cache stays warm). Fix live-vs-git drift first (live export or
  safe apply, R2). Collapse the four AutoOS routing layers to one (worker reads the registry; OmniRoute does failover).
- Measured windows replace defaults: probe accepted prompt size per leg into the registry.
- Gemini quota through agy (Google's CLI / ACP server), never opencode + Antigravity OAuth (ban reports, W1). Qoder through qodercli.
  OpenHands kept as a harness (D17) with its condenser; each harness gets a 10-card A/B before it becomes a default.
- Leg order (D7, D25), cheapest first inside each class: trial -> **qodercli qwen-3.8-flash (free while qoder credits remain, until its
  rate limit)** -> other free legs (opencode zen free, openrouter free, ovh while credits last) -> **mimo-v2.6-flash** (subscription) ->
  **agy: gemini-3.8-flash / sonnet-5.5** (Antigravity subscription; only reachable via agy ACP; gemini after its Friday-evening reset) ->
  deepseek (pay-as-you-go, only if really needed). Sonnet-5.5 via agy is Antigravity quota, not the Claude Max subscription, so D2
  (Max = planner + judge) is not affected; it is still a Claude-family leg for the cross-family review fence.
- Harness choice therefore follows the leg: qwen-3.8-flash -> qodercli; gemini-3.8-flash / sonnet-5.5 -> agy; gateway legs -> opencode.
  The worker treats (harness, model) as one routing target; OmniRoute failover applies only inside opencode targets.
- Claude Code stays on the direct Max subscription for planners/judges; no gateway credentials in those sessions (gateway mode loses
  Max, Remote Control and tool search, W1/W2/W3).

## 6. Communication rules (replaces the messaging part of unattended-orchestration)

- Status is state: workers and engine write events; nobody sends status messages.
- Allowed messages: question, decision, done (milestone), alarm. Questions go to the Inbox, not to an agent.
- No worker-to-worker talk; hub-and-spoke through keeper + engine (cross-host problem solved by pull-based workers).
- Attended Claude sessions that must receive peer messages run with `crossSessionInbound: accept` (W2: bypass-mode receivers hold
  messages). Planner wake from the keeper: a Channel MCP bridge or a Monitor long-poll; decided in P3.
- Experts: a short-lived session started with the project's context pack (generated from keeper + repo index + omnigraph), answers, exits.

## 7. Overview UI (D16, D24)

SvelteKit, all pages converted. Tabs: Plan/DAG; Runs (attempt state, live log tail, cost, link to the Temporal workflow);
Inbox (questions with defaults and deadlines, answer from phone, approvals); Costs (per provider, per project, Claude meter, budget
governor state); Knowledge (omnigraph viewer embedded). One login (existing edge/Authelia path, decided at build time). Interim chat
view for Claude planner sessions = claude.ai/code + Claude app; worker transcripts = attempt logs in Runs.

## 8. Token hygiene (applies to every LLM session)

One card per worker session; byte-stable prompt prefix (sorted tool lists, no mid-session config edits); minimal MCP surface for workers
(serena only when the card touches > 5 files), planners get serena/graphify/omnigraph + OmniRoute MCP read-only (D21); output
filter hook on test/log output (official PreToolUse pattern first); observation masking before summarisation; standing rules re-read
from files after any compaction (summaries keep ~17 % of constraints, W3); skills < 500 lines; CLAUDE.md/AGENTS.md short.

## 9. Budget governor, metrics, M1 targets (D23)

Daily job writes into the keeper: Claude weighted tokens/day, burn vs 1/7 of the weekly meter, per-provider spend and quota, tokens and $
per landed change, wall time per card/phase, operator minutes/day (Inbox activity + attended planner/judge minutes), nudges per card.
Governor: when Claude burn > 1/7/day, no new planner/expert starts except operator-initiated; worker legs throttle by provider quota.
The governor never blocks answering questions, operator-started sessions or a judge run the operator asks for (no deadlock); every
override is logged.
Definitions (RV1/RV2): landed change = a card merged to main through its phase PR; nudge = an operator action required for a card to
change state (Inbox answers, manual retries; planning-session dialogue does not count); persistent session = an LLM session alive
> 15 min outside a planner/expert/judge task or a card attempt.
M1 targets (all must hold; sample >= 20 landed cards AND >= 7 days): weekly meter never hit before reset; >= 70 % fewer Claude
weighted tokens per landed change than the 10-08..10-12 baseline (weights as in metrics-baseline: input + output + cache_write +
0.1 x cache_read); **median card wall time (ready -> merged) <= 50 % of the baseline lane median** (speed-up, D1; added after review);
0 persistent sessions; >= 80 % of cards merged without a nudge; operator <= 60 min/day. Also reported: tokens per estimated hour
(guards against gaming with trivial cards).

## 10. Repos and migration (D13, D-1198 partly superseded)

| Repo | After the rethink |
|---|---|
| plangraph (public after P0 readiness) | keeper (Python -> Rust later), SvelteKit UI, Temporal workflows + Rust worker, spawner substrate (from AutoOS tools), agent skills (`plangraph/skills`, incl. unattended-orchestration rewritten), generic router code (~2k LOC from routing), contracts/schemas, plugins later |
| plangraph-data (private) | plan data until the DB is the only store; site config (hosts, combos policy values, budgets) |
| AutoOS (public) | machine/app installer, catalog/registry, OmniRoute config + apply; installs plangraph at a pinned tag |
| Server | infra MCP stacks (omnigraph, graphify, serena, homelab-mcp) and deployments (Postgres, Temporal, keeper, UI) |
| routing (private) | archived read-only after the decisions cutover + one parallel week |

Supersedes: D-909 AutoOS lanes (AO-ROUTER-PUBLIC, AO-ROUTER-SCRUB-CI, AO-ROUTERCTL-CFG, ROUTING-INTO-AUTOOS, LAYERED-PLANNING-MOVE),
AO-SKILL-INTERFACE (interface not needed once the spawner moves), D-912 (SQLite), PG-RF-COPY/PG-RF-CUTOVER (router home in
plangraph-data/router replaced by "no persistent router"). Kept: PG-PUBLIC-READY (gate before moving code into the public repo),
PG-DEC-* chain (decisions table), PG-NOTIFY-* intent (now Temporal signals + Inbox).
Deletions (after replacement exists): PowerShell runner stack (~6k lines), swarm-orchestration skill (4.9k), one-off diag scripts (~1k),
opencode L0/L1 orchestrator lanes (`oc_l*`, R-router/R-orch-21..28 rules), fleet-revive/fleet-wake/router-heartbeat units,
build-plan-page, md-sync + markdown ledgers (at cutover), one of select_combo v1 / resolver v2, ready-gate cap-exception ceremony.

## 11. Phases (each phase = a gate item in the plan graph; exit criteria are checks)

| Phase | Content | Exit (check) |
|---|---|---|
| P0 Prepare (now, fleet) | fleet slim (D5), md-sync stop-gap + halt alert (D-1297), tooling freeze, baseline metrics job (section 9 numbers for 10-08..10-12 and current), OmniRoute live->git export (drift 0), cancel superseded items | baseline numbers in keeper; drift check 0; md-sync green 24 h |
| P1 Bootstrap (build L1 + Sonnet subagents, D19) | P1.1 Temporal validation (1 day): RAM on coding.vm (else cloud.vm via routing->server), Rust SDK workflow+activity+signal+timer, WSL2 worker reaching the frontend across the 2 networks, UI deep link. P1.2 keeper -> Postgres (Python port, blue-green: old SQLite keeper read-only 48 h, counts + hash chain verified) + new tables + frozen contract suite. P1.3 projector (Temporal history -> keeper events) + nightly reconciler. P1.4 Rust worker MVP: opencode adapter, worktree, checks, confinement, outcome probe, failure classes + budgets, advisory-lock merge with recheck, janitor. P1.5 window-class combos in OmniRoute. P1.6 planner kit: card format + readiness gate + keeper-side routing + context pack + budget governor job. P1.7 minimal Inbox: question/answer API + a minimal answer form (moved from P2/P3 after review: P2 needs answers). P1.8 phase-review harness + JSON verdict schema (moved after review) | one real card from the plan graph runs end-to-end on a free leg, passes its check, merges, shows in the keeper; one test question answered through the minimal Inbox resumes its run |
| P2 M1 dogfood | P2.1 worker harness adapters **qodercli** (qwen-3.8-flash first leg) and **agy** (gemini-3.8-flash, sonnet-5.5 via ACP) + leg order qwen-3.8-flash -> mimo-v2.6-flash enforced in routing, each with a 10-card A/B against opencode on the same cards (D25); P2.2 the pipeline executes its own next phase: keeper decisions table live and every writer switched to it (no new markdown writes; deletion is P4) | adapters pass their A/B cards; M1 targets met on >= 20 landed cards and >= 7 days |
| P3 Operator surface | SvelteKit conversion of all pages; Runs/Inbox/Costs/Knowledge tabs; answer path + phone push; Temporal deep links; planner wake (Channel or long-poll) | operator answers a question from the phone and the run resumes |
| P4 Consolidation | the single ledger cutover (markdown ledgers archived read-only, md-sync deleted); routing generic code -> plangraph; routing archived; skills -> plangraph/skills (unattended-orchestration as <=150-line index + role cards; mechanical rules -> code; CI lint rejects skills > 500 lines); infra MCP -> Server; AutoOS deletions; PG-PUBLIC-READY then the operator flips plangraph public | CI job clones the released plangraph tag, runs a fresh setup (Linux + WSL2) and one bootstrap card end-to-end with zero errors |
| P5 Widen | OpenHands harness A/B; Jev shadow evaluation; second project; unattended planning for well-understood phases on free planner legs; OmniRoute MCP read-only pilot; plugin packaging | second project phase meets M1 targets |
| P6 End state | Rust keeper against the frozen contract; control plane move to the server/cloud.vm (D-907) | contract suite green on the Rust keeper |

## 12. Risks

| Risk | Mitigation |
|---|---|
| Temporal Rust SDK young (GA Sept 2026) | 1-day validation; fallback: Temporal worker in another SDK for the engine-facing part, or own Rust runner behind the same seam |
| coding.vm memory (16 GB, swap near full) | measure in P1.1; cloud.vm per D10 |
| Network reach workstation -> coding.vm (two networks, ShieldsUp) | P1.1 item; reuse the Caddy edge + mTLS path the keeper already uses; no Funnel (D21) |
| Workflow determinism violated by agent-written code | Temporal replay tests in CI; workflows small, logic in activities |
| Cards without good checks (planner quality) | D8 + readiness gate; attended planning; held-out checks for risky cards |
| Free legs rate-limited | durable timers, class combos with several legs, subscription legs next, deepseek last |
| Public repo leakage when spawner/skills move | PG-PUBLIC-READY gate (names list, gitleaks, site strings to config) before any move |
| Big-bang migration | strangler order: new path runs beside the old fleet; old parts deleted only after replacement checks pass |
| Provider ban / ToS enforcement mid-run (agy, qoder, free legs; W1 ban reports) | per-provider kill switch in routing; agy only via its own CLI/ACP; cards re-route to the next leg; alarm in the Inbox |
| Single Postgres for keeper + Temporal | WAL archiving + point-in-time recovery, nightly off-host backup, monthly restore drill (RT-P1-PG-DEPLOY) |
| Untrusted agent code on the workstation | WSL2 user without host secrets; agents run in the existing jail/sandbox; legs get only their own credential; outside-path fence kept |
| Worker upgrades break long-running workflows | Temporal worker versioning (build ids) + replay tests in CI; harness versions pinned per run, upgraded only as an A/B lane (also keeps prompt prefixes and caches stable) |
| Merge lock serialises phases | lock covers only the merge step; per-repo locks; measured in M1 |
| M1 metric gaming with trivial cards | tokens per estimated hour reported next to tokens per landed change; judge samples cards at the M1 verdict |
| Keeper migration data loss (P1 Python->Postgres, P6 ->Rust) | blue-green, old keeper read-only for 48 h, row counts + hash chain compared before cutover |

## 13. Evaluation of the pasted proposal (summary; detail in E1 and the handoff)

Kept: readiness separated from routing; single-axis questions; positive write-set confinement; rebase under a merge lock; planner exits
after a phase; attended/unattended planning mode; phase-level review; context-sized batches; frontier one-shot + outer gate as an
experiment. Rejected or corrected: plan data and project contracts inside the public plangraph repo; YAML files as run state; L3 workers
on coding.vm (D-437); Open-Jev-27B for L0 (no GPU); an SSE spawn endpoint that does not exist; an invented combos.json schema and
"nvidia_trial gemini"; Antigravity via opencode; diff truncation at 120k chars and string-matched verdicts; Sonnet as an L3 worker
(contradicts D2). Unverified claims flagged: "Opus/Sonnet/Gemini 10/10" is not in the Jain article; "AAD" confidence not found; inner vs
outer sensor scheduling is an untested hypothesis.

## 14a. Cross-family reviews and how they were folded in

Reviews of this design at fcec215, run tool-less through the gateway by workst-L1-autoos: RV1 ovhcloud/gpt-oss-120b (call-log
1792074414302-51decd) and RV2 xiaomi-mimo/mimo-v2.6-pro (call-log 1792074414313-79e8ec), both "approve-with-changes"; full texts in
`docs/research/rethink-2026-10-15/RV1-*.txt`, `RV2-*.txt`.
Accepted: single owner per fact + projector + reconciler (section 2); lease/heartbeat numbers, exit classes, retry budget, flaky detector,
usage-limit re-route/cap/jitter, zombie fencing (4.5); advisory-lock merge with recheck + PR to main (4.6); review verdict schema (4.7);
risk-tagged questions without defaults (4.8); "leg tier" vs "window class" naming; routing as keeper-side code; minimal Inbox + review
harness moved into P1; ledger cutover only in P4; metric definitions, sample size and a wall-time target (9); governor never blocks
answers/operator sessions; concrete P4 exit CI job; risk rows (12).
Not accepted: swap P2/P3 or a Slack webhook (RV1) - the minimal Inbox in P1 solves the dead end; making a Postgres SKIP-LOCKED queue the
default instead of Temporal (RV2) - the operator chose Temporal after the comparison (round 5); that queue stays the documented fallback
of the 1-day validation; "single host for control plane + worker first" (RV2) - conflicts with D-437; replacing the SvelteKit conversion
by a read-only dashboard (RV2) - operator decision D24 (the P1 minimal Inbox form covers the interim).

## 14. Defaults taken without a separate question (operator may override)

Budget governor on; planners on direct Max subscription; crossSessionInbound accept for attended sessions; plugins after the skills cut
(P5); unattended planning only after M1; OmniRoute config stays in AutoOS; strangler migration order; PG-PUBLIC-READY before code moves.
