# M2c dashboard research (2026-10-01)

## Question
The plan graph becomes the operator's MAIN dashboard (D-231): project clusters -> phases -> tasks, with questions, decisions and
operator tasks as first-class blocking items; a web GUI that can do everything the keeper API can, as the authenticated operator
(Authelia OIDC, CSRF-safe, every action an attributed event, DONE locked); per-task/phase/project model, cost and time insight.
What do current tools and standards say is best practice? Not duplicated: round 1/2 files in `routing/research/`.

## Method
Six Haiku researchers (general-purpose, WebSearch/WebFetch, read-only), one per question R1-R6, each limited to about 25 tool
calls and told to cite only fetched pages and mark the rest UNVERIFIED. No Sonnet re-runs (the token budget was already exceeded,
see below). Orchestrator (Sonnet) merged the six reports and did NOT re-fetch the sources; claims therefore rest on the
researchers' fetches. "Seen" date for every source = 2026-10-01. Confidence flags:
- `[n]` = cited by a researcher as fetched. `(sec)` = the source is a third-party blog/aggregator, not a vendor or spec doc.
- UNVERIFIED = the researcher said so, or the source cited does not identify a fetchable page, or I judge the claim not
  supported by the cited URL.

| researcher | question | model | approx tokens |
|---|---|---|---|
| R1 | operator dashboards for agent fleets | haiku | 48.6k |
| R2 | human-in-the-loop approval UX | haiku | 39.9k |
| R3 | graph views 100-500 nodes, desktop + phone | haiku | 40.0k |
| R4 | cost/time/telemetry presentation | haiku | 42.5k |
| R5 | secure GUI write path behind Authelia | haiku | 37.3k |
| R6 | data model: blocking questions/decisions | haiku | 41.1k |

Budget note: researchers alone used about 249k tokens against a 150k brief cap (the six ran in parallel and each did 20-34
tool calls). Nothing further was spawned. Quality is uneven: R3, R5 and R4 cite mostly primary docs; R1, R2 and R6 lean on
secondary sources in places (flagged).

## Findings

### R1 Operator dashboards for agent fleets
1. LLM-observability tools open on aggregate health, not on a task list. Langfuse: customizable dashboard (traces, model cost,
   latency p50/95/99, scores, model usage), click through to a span tree [1]. LangSmith: prebuilt dashboard with six
   sections: traces, LLM calls, cost and tokens, tools, run types, feedback scores [3]. AgentOps: fleet overview listing every
   agent with cost per run, tokens, cost by model/workflow, run- and step-level drill-down [5].
2. Workflow engines open on a filterable run list. Temporal: "Recent Workflows" list within retention; grouped by namespace,
   workflow type, status, custom search attributes; up to 20 saved filter views [11]. Inngest: Runs page across apps, filter by
   environment/status/app, CEL search over event fields [15]. Trigger.dev: task overview with metrics, run list and a 24h
   activity chart; live filters by status, env, tags, date; payload JSON-path columns [17].
3. Orchestrators open on an overview with tabs. Dagster: Overview ("factory floor") with Runs, Jobs, Schedules, Sensors,
   Resources, Backfills; per-run Gantt of step durations [13]. OpenHands: recent projects and an automations dashboard with
   run metadata [7]. Gas Town: convoy viewers and dashboards over Beads work units that agents claim [9][10] (the claim that
   it is MIT and open-sourced Jan 2026 is from [10]; the X post [9] is a pointer only).
4. Grouping in all of them is flat-plus-filters (tags, metadata, environment, status), not a project -> phase -> task tree.
   No researched tool shows a plan hierarchy with blockers on the first screen; the closest is Beads "Needs You" (see R6 #8).
   This is a gap the plan graph fills, not a pattern to copy.
5. Self-hosting / licence as reported: Langfuse MIT [2]; AgentOps MIT with a self-hosting guide [6]; OpenHands MIT core,
   enterprise dir PolyForm Free Trial (sec [8]); Gas Town MIT [10]; Temporal MIT (cost figure of $400-900/mo is from a
   third-party page, sec [12], UNVERIFIED); Dagster Apache-2.0 (sec/vendor blog [14]); Inngest SSPL server with Apache-2.0
   SDKs [16]; Trigger.dev Apache-2.0 [18]; LangSmith platform proprietary, self-host needs Enterprise [4].
   Cost/time: none of these is needed as a dependency; they are UX references only.

### R2 Human-in-the-loop approval UX
1. LangGraph `interrupt()` halts the graph, persists state, resumes with the same thread_id and `Command(resume=value)`; the
   node re-executes from its start on resume, so work before the interrupt must be idempotent [19].
2. Temporal approvals are signals/conditions: workflow blocks on `wait_condition` with a timeout; the signal carries a structured
   approval (approver id, decision, comment, timestamp) [20].
3. n8n pauses a run and stores state; a signed one-time resume URL wakes it; approvals via send-and-wait on messaging nodes,
   Wait + IF, or AI tool gating (sec [21], not the n8n docs; UNVERIFIED against n8n docs).
4. GitHub environments with required reviewers block a job until approved, approval buttons in the GitHub UI; custom
   protection rules via GitHub Apps; unapproved jobs fail automatically (researcher says after 30 days) [22].
5. Agent-inbox pattern: interrupts route to an owner's queue; risky actions default to "ask" (sec [23]; the LangChain
   Agent Inbox repo itself was not fetched, UNVERIFIED).
6. HumanLayer: `require_approval` decorator blocks a call, denial feedback goes back to the LLM, routing/escalation/timeouts
   over Slack, email, SMS, plus a human-as-tool call for questions (sec [24]; the HumanLayer docs/repo were not fetched).
   The researcher found no successor product: UNVERIFIED.
7. Attribution: record reviewer identity, timestamp, what the reviewer saw, outcome; if the agent changes target/amount after
   approval, a new approval is needed; say which agent decided vs executed (sec [25]).
8. Corrections: approved items lock; a correction is a new entry that references the original; enforce with triggers or revoked
   UPDATE/DELETE, hash-chain to detect tampering (sec [26]; these are vendor blogs, but they match the keeper design).
9. Mobile: notify -> pause -> resolve; lock-screen push (e.g. ntfy) with one-tap approve/reject via a signed webhook; approvals
   persisted so they survive restarts (sec [27], single vendor blog, UNVERIFIED as best practice). Slack-first routing is
   claimed for on-call contexts (sec [28]).
10. Net: first-class blocking human items exist as a pattern in every framework, but the primary-doc coverage fetched is
    thin (LangGraph [19], Temporal [20], GitHub [22]); undo/correction and mobile findings rest on blogs.

### R3 Graph views at 100-500 nodes, desktop and phone
1. Cytoscape.js canvas renderer: 286 nodes/288 edges rendered in about 50 ms (about 20 FPS cap); 2,643 nodes/4,903 edges about
   5 FPS on an i7-9700 at 4K [29][30]. So 76-500 nodes is inside the comfortable range; thousands is not.
2. Experimental WebGL renderer (v3.31 preview, provisional API): 1,200 nodes/16,000 edges 20 -> 100+ FPS; weak text rendering,
   limited edge/arrow styles [29]. Preview status: do not depend on it.
3. Layered layout: dagre (MIT, v4.0.1, last update about a month ago, known problems with compound nodes) versus ELK (MIT,
   v2.3.0, 7 algorithms, better compound support, slower and larger, transpiled from Java). Versions/health scores come from
   npm.io pages (sec [32][33]); design discussion from the Cytoscape layouts blog [31]. Versions UNVERIFIED against npm.
4. cytoscape-expand-collapse gives collapse/expand with animation and re-layout, suited to cluster -> phase -> task, but the
   README says it is no longer maintained (new framework in development) [34].
5. Official perf guide: `hideEdgesOnViewport`/`textureOnViewport` are now "largely moot"; use `min-zoomed-font-size` and
   zoom thresholds for semantic zoom/level of detail; `pixelRatio: 1` helps big graphs; prefer `getElementById`, `cy.batch()`,
   few animations, no function-valued styles, haystack edges, no dashed lines [35].
6. Touch: Cytoscape abstracts mouse/touch (pinch-zoom, pan, tap, taphold); some Chrome versions broke touch/box selection [36].
7. Dagster scaled DAG view to 2,000 assets by caching layouts in IndexedDB (initial 5 s -> instant), drawing only visible
   edges, and replacing an O(N^3) ranker with tight-tree [37]. Layout cost, not rendering, is the bottleneck to cache.
8. No source gives a standard phone fallback for graphs. Findings suggest (not stated by a source): list/inbox below about
   480 px. The researcher's mention of an Airflow mobile app is UNVERIFIED (no source cited).

### R4 Cost, time and telemetry presentation
1. Langfuse stores `usage_details` (input, output, cache_read, audio, reasoning) and `cost_details` per generation; model
   pricing matched by regex; grouping by model, tag, user, time [38]. Helicone groups by custom properties passed as
   `Helicone-Property-*` headers and `Helicone-User-Id` [39]. OpenRouter returns a `usage` object with token buckets, `cost`,
   `cost_details.upstream_inference_cost` on every response; history via the `/generation` endpoint [40]. LiteLLM spend logs
   hold key, user, team, model_group, api_base, spend, token counts, `request_tags`; reports by team/customer/key/model/
   provider; custom `spend_logs_metadata` is an enterprise feature [41].
   Shared pattern: cost is attached per request and rolled up by a caller-supplied tag; a per-task roll-up needs a task tag.
2. OpenTelemetry GenAI conventions (status: Development, i.e. unstable): `gen_ai.provider.name`, `gen_ai.request.model`,
   `gen_ai.response.model`, `gen_ai.operation.name` (chat, invoke_agent, execute_tool, invoke_workflow, ...),
   `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, cache read/creation and reasoning token attributes,
   `gen_ai.response.finish_reasons`. No standard cost attribute was found in this registry page [42].
3. Definitions: Kanban cycle time = active work until ready for delivery; lead time includes waiting from request; throughput =
   items finished per period; cycle time = WIP / throughput (sec [44]). DORA lead time starts at first commit (sec [43]).
   Both sources are secondary, not Kanban Guide/DORA primaries, UNVERIFIED as authoritative. Mapping to the keeper: lead =
   created -> done, cycle = claimed -> done (our inference).
4. Claude Code exports OpenTelemetry metrics `claude_code.cost.usage`, `claude_code.token.usage`, `claude_code.session.count`,
   `claude_code.active_time.total` and others, and events `claude_code.api_request`, `api_error`, `tool_result`, ... Attributes
   include `session.id`, `model`, `query_source` (main/subagent/auxiliary), `effort`, `agent.name`, token `type`
   (input/output/cacheRead/cacheCreation); API-request events carry cost, token counts, `duration_ms`; correlation via
   `prompt.id`, `request_id` [45]. A session or request id is the join key to a task, not a task id: the task id must be
   injected by us (our inference).
5. Local logs: per-session JSONL under `~/.claude/projects/<project>/<session>.jsonl` with token counts by type and model [46].
   The researcher's source is a different tool (claude-code-usage-analyzer), not ccusage itself; ccusage's own README was not
   fetched: UNVERIFIED for ccusage specifics.

### R5 Secure GUI write path behind Authelia
1. Authelia is an OIDC 1.0 provider with the authorization-code flow; PKCE `public_clients_only` (default) or `always`; client
   auth `client_secret_basic`/`client_secret_post`/`none`; exact, case-sensitive redirect URI matching; claims
   `preferred_username`, `groups` via scopes; authorization policy can require one_factor/two_factor per client [47][48].
2. Forward-auth injects unsigned `Remote-User/Groups/Name/Email`; Authelia's threat model says anyone on the network who can
   reach the app directly could send those headers and bypass auth, so network isolation is mandatory; header signing is only
   a roadmap item [49]. OIDC avoids this because the app validates a signed token itself [47] (inference from [47][49]).
3. OWASP CSRF cheat sheet: synchronizer token or signed double-submit cookie; `Sec-Fetch-Site` rejection of cross-site unsafe
   requests; custom request header; Origin/Referer verification; SameSite as defence in depth; none of it survives XSS [50].
   Researcher recommendation for a JSON API: Fetch-Metadata + custom header, token optional. This is a judgement, not a quote.
4. OWASP session guidance: HttpOnly, Secure, SameSite, `__Host-` prefix, idle plus absolute timeout, regenerate the id on
   privilege change [51]. (The researcher attributed rotation to [49]; the threat-model page is not about sessions, so cite [51].)
5. OAuth security BCP and browser-based-apps drafts: state/nonce/PKCE, short-lived tokens, refresh-token rotation or
   sender-constraining [52][54]. [52] was cited as RFC 9700 but the URL is the draft; UNVERIFIED which text was read.
6. Principal mapping: key the operator on the immutable `sub`, treat `preferred_username` as display only (this is OIDC Core
   practice; no fetched page states it, UNVERIFIED as cited). Log identity, action, result, reason, time, source IP [53].
   Keep the browser session (cookie) separate from the agent Bearer-token path (the keeper already uses Bearer; [54] frames it).

### R6 Data model: clusters, phases, tasks, blocking questions/decisions
1. Linear: Initiatives > Projects > Milestones > Issues > Sub-issues; "blocks/blocked by" relations, which drop to "related" when
   the blocker closes [55]. Plane: Project > Cycles, Modules, Epics > Issues [56]. OpenProject: typed work packages (Task,
   Phase, Milestone), parent-child plus relations; "blocks" constrains status (cannot close), predecessor/successor constrains
   schedule [57]. Jira: "blocks/is blocked by" prevents closing, extra link types via Advanced Roadmaps [63].
2. ADR tooling: MADR status proposed -> accepted -> deprecated/superseded; after acceptance only status changes, a new ADR
   supersedes the old via cross-links [58][60]; adr-tools uses the same status set [59]. This is the correction model the
   keeper already wants (new linked item, never edit).
3. Beads has issue types decision, gate, human gate, plus dependency types blocks, parent-child, conditional-blocks, waits-for;
   a "Needs You" section lists open human gates [62] (the "Needs You" detail is from this single page).
4. GitHub Discussions separates questions from issues [61]: questions live outside the task tracker. Contrast: our model puts
   them in the graph so they block.
5. Migration patterns (sec [64] plus one unsourced item): keep stable IDs (D-NNN, ADR-NNN) as immutable source ids, back-link,
   freeze the old file with a dated pointer rather than deleting it. A JSON Lines layout for QUESTIONS.md (fields n, at,
   stream, kind, gate, asked, answer, grounds, agent) was reported with source "GitHub issues (pattern examples)": no URL,
   UNVERIFIED; treat as a design suggestion only.
6. Net: every mainstream tool has "blocks" as a status constraint and decisions as supersede-not-edit; none treats
   "question awaiting the operator" as a node on the dependency graph except Beads gates [62].

## Implications for M2c
(what to adopt, avoid, ask; no proposal text)
1. Adopt "Needs You" as the landing view: open questions, decisions and operator tasks that block something, sorted by how
   much work they unblock; then ready, then in progress. No researched dashboard has a hierarchy-with-blockers home screen
   (R1 #4), so this is own design; Beads' human gate is the nearest precedent (R6 #3).
2. Treat question/decision/operator_task as `kind` values on existing items with a `depends_on` edge from the blocked work;
   answering is an event, not an edit. Fits Linear/OpenProject "blocks" semantics (R6 #1) and the existing keeper tables.
3. Corrections of decisions as new items that supersede the old (MADR/log4brains, R6 #2; R2 #8): consistent with DONE-locked.
4. Graph: Cytoscape canvas is adequate for 76-500 nodes (R3 #1); keep WebGL out. Cache layouts and render only what is
   visible; the first-paint cost is layout, not drawing (R3 #7).
5. Layout choice needs a day-1 test on real plan data: dagre is lighter, ELK handles compound nodes better (R3 #3).
   Compound collapse plugin is unmaintained (R3 #4): avoid it or vendor and own it; simpler alternative is collapsing by
   rebuilding the graph per cluster/phase (our suggestion, untested).
6. Phone: the graph is secondary. Ship the list/inbox view as the default under about 480 px and put approve/answer on it;
   no source gives a standard, so test on the real phone (R3 #8, R2 #9).
7. Operator write path: prefer OIDC authorization-code + PKCE over trusting `Remote-User` (R5 #1-2); if forward-auth is kept,
   the keeper must be unreachable except via the proxy (R5 #2).
8. CSRF: `__Host-` cookie, SameSite, Origin plus Sec-Fetch-Site check, custom header on every state-changing request, signed
   token as the last layer (R5 #3-4); only one operator principal maps from the OIDC `sub`, agents stay on Bearer tokens (R5 #6).
9. Every GUI action is an event with actor = the operator principal and method = `gui`, plus source IP and the OIDC `sub`
   in the payload (R5 #6, R2 #7); record what the operator saw (item epoch/version) so a stale approval is refused (R2 #7).
10. Cost: store per-call usage with a task key supplied by the gateway/agent (tag or header), as Helicone/LiteLLM/Langfuse do
    (R4 #1); do not expect Claude Code's own telemetry to know the task id (R4 #4).
11. Use OTel GenAI attribute names (`gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.usage.*`) for column naming, but
    note they are in Development status and carry no cost attribute (R4 #2).
12. Time metrics: define cycle = claim -> done and lead = created -> done, plus review rounds from `changes_requested` events and
    throughput per week; cite a primary Kanban/DORA source when the proposal fixes the definitions (R4 #3, UNVERIFIED).
13. Avoid adopting any researched fleet dashboard wholesale: all are run-observability tools with flat grouping (R1 #4); use
    them as UX references only.
14. Open for the operator: (a) mobile push for new blocking items (ntfy or similar) in scope for M2c? (R2 #9, weak source);
    (b) is OIDC mandatory or may forward-auth stay for a first version? (c) are cost sources limited to the gateway ledger plus
    Claude Code OTel/JSONL, and who tags tasks? (d) how are historical decisions (D-NNN) and QUESTIONS.md imported: as
    `import_items` legacy rows or generated views (R6 #5)?
15. Research gaps to close before the proposal if wanted: primary docs for n8n, HumanLayer, Agent Inbox, ccusage, Kanban
    Guide/DORA, OIDC Core `sub` rules, RFC 9700 text; one Sonnet pass on R2 would cover the first three.

## Sources (all seen 2026-10-01)
1. https://langfuse.com/blog/2025-05-21-customizable-dashboards
2. https://langfuse.com/handbook/chapters/open-source
3. https://docs.langchain.com/langsmith/dashboards
4. https://docs.langchain.com/langsmith/self-hosted-changelog
5. https://www.agentops.ai/
6. https://docs.agentops.ai/v2/self-hosting/overview
7. https://docs.openhands.dev/openhands/usage/cli/web-interface
8. https://aiagentplatforms.dev/platforms/openhands/ (sec)
9. https://x.com/Steve_Yegge/status/2031464944072536183
10. https://yegge.ai/gastown
11. https://docs.temporal.io/web-ui
12. https://automationatlas.io/answers/temporal-pricing-explained-2026/ (sec)
13. https://docs.dagster.io/concepts/webserver/ui
14. https://dagster.io/blog/open-core-business-model-dagster
15. https://www.inngest.com/docs/platform/monitor/inspecting-function-runs
16. https://www.inngest.com/blog/open-source-event-driven-queue
17. https://trigger.dev/changelog/dashboard-ui-updates
18. https://trigger.dev/docs/introduction
19. https://docs.langchain.com/oss/python/langgraph/interrupts
20. https://docs.temporal.io/design-patterns/approval
21. https://rills.ai/blog/n8n-human-approval-workflow ; https://n8n.io/workflows/6269-state-management-system-for-long-running-workflows-with-wait-nodes/ (sec)
22. https://docs.github.com/actions/deployment/about-deployments/deploying-with-github-actions
23. https://www.patternautomation.com/pattern-automation/news/give-your-langchain-agent-a-real-inbox/ (sec)
24. https://www.everydev.ai/tools/humanlayer (sec)
25. https://www.runlayer.com/blog/ai-agent-audit-trails-dos-and-donts ; https://www.augmentcode.com/guides/multi-agent-outputs-n-pass-enterprise-audit (sec)
26. https://www.sirion.ai/library/contract-insights/approval-audit-trail-explained/ (sec)
27. https://agentfield.ai/blog/approve-from-your-phone (sec)
28. https://www.agentic-patterns.com/patterns/human-in-loop-approval-framework/ (sec)
29. https://blog.js.cytoscape.org/2025/01/13/webgl-preview/
30. https://github.com/cytoscape/cytoscape.js/discussions/3088
31. https://blog.js.cytoscape.org/2020/05/11/layouts/
32. https://npm.io/package/cytoscape-dagre (sec)
33. https://npm.io/package/cytoscape-elk (sec)
34. https://github.com/iVis-at-Bilkent/cytoscape.js-expand-collapse
35. https://github.com/cytoscape/cytoscape.js/blob/master/documentation/md/performance.md
36. https://github.com/cytoscape/cytoscape.js/issues/1702
37. https://dagster.io/blog/scaling-dag-visualization
38. https://langfuse.com/docs/observability/features/token-and-cost-tracking
39. https://docs.helicone.ai/features/advanced-usage/custom-properties
40. https://openrouter.ai/docs/guides/guides/administration/usage-accounting
41. https://docs.litellm.ai/docs/proxy/cost_tracking
42. https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/
43. https://www.projectmanagement.com/wikis/1122558/modern-metrics--dora-metrics--flow-metrics--lead-time--cycle-time--throughput- (sec)
44. https://www.nimblework.com/agile/lead-time-cycle-time/ (sec)
45. https://code.claude.com/docs/en/monitoring-usage
46. https://github.com/aarora79/claude-code-usage-analyzer (not ccusage)
47. https://www.authelia.com/configuration/identity-providers/openid-connect/provider/
48. https://www.authelia.com/configuration/identity-providers/openid-connect/clients/
49. https://www.authelia.com/overview/security/threat-model/
50. https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
51. https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
52. https://datatracker.ietf.org/doc/html/draft-ietf-oauth-security-topics
53. https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html
54. https://datatracker.ietf.org/doc/html/draft-ietf-oauth-browser-based-apps
55. https://linear.app/docs/issue-relations
56. https://docs.plane.so/
57. https://www.openproject.org/docs/user-guide/work-packages/work-package-relations-hierarchies/
58. https://adr.github.io/madr/
59. https://adr.github.io/adr-tooling/
60. https://github.com/thomvaill/log4brains
61. https://docs.github.com/en/discussions
62. https://beads.gascity.com/core-concepts/dependencies
63. https://confluence.atlassian.com/spaces/JIRASOFTWARESERVER/pages/1044784190/Dependencies+in+Advanced+Roadmaps
64. https://software-architecture-guild.com/guide/architecture/documentation/architectural-decision-records/ (sec)
