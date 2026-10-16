# Owner reassignment 2026-10-16 (D-1519)

plangraph-L1-plan2, for the router. Built on the live keeper snapshot 2026-10-16T20:19:49Z (head_seq 6607).
Batch: `2026-10-16-owner-reassign.yaml`. Edits the field `owner` only; no dependency changes, no cancels.

## Rules

- Every open item owned by a session that no longer exists moves to the live owner named in D-1519:
  autoos-L1-main -> workst-L1-autoos, plangraph-L1-main -> build-L1, server-L2-maint -> server-L1-main,
  chaptarr-L1 -> server-L1-main, no owner -> the project's L1 (autoos -> workst-L1-autoos, server -> server-L1-main).
- All open items of those owners move, not only the stale-ready ones, so the blocked rest does not go stale later.
  The column `stale-ready` marks the subset routerctl showed (all deps done, no update for 6 h).
- Two free-text variants of autoos-L1-main move too (`autoos-L1-main (L2 gateway lane)`, `autoos-L1-main (L2)`).
- Claimed and in-review items are locked for edits and stay as they are: `autoos:T0-SYNC` (in_review, AutoOS L1-main -> L3).
- Cancels: none. The triage left no CANCEL verdict open on these items.
  `plangraph:PG-PUBLIC-READY` is COVERED in the triage, which means it is already a dep of RT-P4 gates and stays.

## Counts

| From | To | Items | Stale-ready |
|---|---|---:|---:|
| - | server-L1-main | 46 | 17 |
| - | workst-L1-autoos | 4 | 0 |
| autoos-L1-main | workst-L1-autoos | 136 | 81 |
| autoos-L1-main (L2 gateway lane) | workst-L1-autoos | 1 | 0 |
| autoos-L1-main (L2) | workst-L1-autoos | 1 | 0 |
| chaptarr-L1 | server-L1-main | 28 | 9 |
| plangraph-L1-main | build-L1 | 46 | 24 |
| server-L2-maint | server-L1-main | 30 | 29 |
| **total** | | **292** | **160** |

## Items

| Item | Kind | From | To | Stale-ready | Triage |
|---|---|---|---|---|---|
| `autoos:ACK-PROBE-BUDGET` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:AIPRO-LIMITS` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:AO-400LOC` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-APIDOCS` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-APPLY-CLUSTER-013` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-AUTO-FANOUT` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-AUTOOS5-REBUILD-DRIFT` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-CHANGELOG-DOCS` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-CLAUDE-SEAT-BASH` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-COMBO-FREE-FIRST` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-FREE-PRIVACY-CLASS` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-GATE-TK` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:AO-GROK` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-HAIKU` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-HAIKU-EXPLORE` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:AO-HAIKU-GW` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:AO-HOSTMIRROR` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-JULES-PIPELINE` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-L2-SEAT-INTEGRITY` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:AO-LAGUNA-SEAT-CAP` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-LEDGER-DRYRUN` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-MCP-CWD-INDEPENDENT` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-MIMO-WIRING` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-OCRUNTIME` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-OPENCODE-JAIL` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-PREPUSH-HOOK-GITENV-2` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-PRICE-GPTOSS120B` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-PROBE-397B-TOOLS` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-QODER-ROUTE` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-READY-MULTI-BRIEF` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-REPORT-SCHEMA` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-RESEARCH-GLM-CODING-PLAN` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-RETIRE-AGENT-SKILLS-CLONE` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:AO-RETRY-GATES` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-REVIEW-OF-XCHECKOUT` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-ROLE-GATE` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-SKILLRULES` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:AO-SKILLS` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-SPAWN-FANOUT` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-SPAWN-KEY-PRESET` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-SPAWN-PATCH-ID` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:AO-SPAWN-PIN-UX` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-SPLIT1` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:AO-SYNC-PARTIAL-PULL-RESTORE` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-TASKKEY` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-TERSE` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-TOOLHOME-SIZE` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-VIDEO-GATE` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-WRITEBACK` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:AO-WRITER-FAMILY` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AO-WSCONFIG` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:AUTOOS-UI-PROFILES` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:BASHGUARD-ORCH-HARDEN` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:BAZAARLINK-AUTODISABLE` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:CHEAPEST-PROVIDER` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:COMBO-REFRESH` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:COMBO-REFRESH-A4` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:CREDIT-18` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:DEVIN-BRIDGE` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:DOCS-OMNIROUTE` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:FA-3` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:FAMILY-TRACK` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:GEMINI38` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:GWRETRY3` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:JULES-CLOUDAGENT` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:LAUNCHER-CPU-GATE` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:LEAN-MCP` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:MCP-REMOTE` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:MCP-SEAT-EVIDENCE` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:MEMGUARD` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:OMNIUPDATE` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:OR-FREE-GUARD` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:OVH-GUARD` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:PH-AO-FLEET` | gate | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:PH-MCP-ONE` | gate | - | workst-L1-autoos | no | INDEPENDENT |
| `autoos:PH-OPS-20261002` | gate | - | workst-L1-autoos | no | INDEPENDENT |
| `autoos:PH-ROUTESETUP` | gate | - | workst-L1-autoos | no | INDEPENDENT |
| `autoos:PH-WS-SHIFT` | gate | - | workst-L1-autoos | no | INDEPENDENT |
| `autoos:PROBE-LEDGER-ATOMIC` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:PROV-WIRE-FREE` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:PROVROUTE` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:PUSHGATE-TIGHTEN` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:ROUTE-FRESH` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:RS-AA-SCORES` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:RS-CHANGELOG-GATE` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:RS-FUTURE-MODELS` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:RS-GEMINI-PARAMS` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:RS-GEMINI38-LEGS` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:RS-LEDGER-WIRING` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:RS-MODELS-DOC` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:RS-NESTED-COMBOS` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:RS-NSCALE` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:RS-OPENCODE-MODELS` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:RS-PLAN-ROWS` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:RS-RENAME-CI` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:RS-SPAWN-TEMPLATES` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:SKILL2MCP` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:SPAWN-LONGPATH` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:SPAWNER-F0-CWD` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:T0-PAID` | lane | autoos-L1-main (L2 gateway lane) | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T1-CLEAN` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T1-COMBO3` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T1-CREDIT` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T1-CTX` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T1-LITELLM` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T1-MM` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T1-NOISE` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T1-PEER` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T1-PROV` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T1-UPGRADE` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T1-WEBTOOLS` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T1-ZEN` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T2-CANARY` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:T2-COMPACT` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T2-LEAN` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T2-MCPLIFE` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T2-ONEPATH` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T2-RECORD` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T2-RECORD-GAPS` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:T2-RESEARCHCARD` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T2-XSECRET` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:T3-CIGREEN-WIN` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:T3-CLEAN` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T3-PREPUSH` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:T3-PRINCIPLES` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T3-SCRUB` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:T3-SPLIT` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T3-WIN` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T4-FENCE` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T4-HARDEN` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T4-ZEN` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:T7-GRAPH-A` | lane | autoos-L1-main (L2) | workst-L1-autoos | no | INDEPENDENT |
| `autoos:T7-GRAPH-USE` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:TIER-CONTRACT` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:U12` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:WRITER-BENCH` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:WS-1` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:WS-6` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `autoos:ZCODE` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:ZCODE-CLIENT-LANE-Z` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `autoos:ZCODE-GLM-BURN` | lane | autoos-L1-main | workst-L1-autoos | no | INDEPENDENT |
| `plangraph:API-OIDC` | lane | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:KG-ADAPTER` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:KG-VIEW` | lane | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:M2B-OFFHOST` | lane | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:MODEL-HOST` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:MODEL-LOOP` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:MODEL-NAMES` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:MODEL-STATS` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PG-ANSWER-UI` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PG-CI-FORGEJO` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PG-DEC-ATTRIBUTION` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PG-DEC-COUNT` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PG-DEC-EDIT-NOTICE` | lane | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:PG-ESTIMATE-SRC-EDIT` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PG-INBOX` | lane | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:PG-L1-CREATE-WITH-EDGE` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PG-L2-SMALLFIX-RULE` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PG-LE-DEPS-OPS` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PG-LE-DROW-LINT` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PG-LE-GUARD-KEY` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PG-M2B` | lane | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:PG-M3` | lane | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:PG-NEEDS-YOU` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PG-OMNIGRAPH-UI` | gate | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:PG-PROJECT-PRIORITY` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PG-PROXY-ACCESS` | lane | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:PG-PUBLIC-READY` | lane | plangraph-L1-main | build-L1 | yes | COVERED |
| `plangraph:PG-QUESTION-CARDS` | lane | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:PG-ROUTER-REOPEN` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PG-UI-BRIEF` | lane | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:PG-UI-TABICON` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PH-M2C` | gate | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:PH-M2C-API` | gate | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:PH-M2C-KG` | gate | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:PH-M2C-MODEL` | gate | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:PH-M2C-SPLIT` | gate | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:PH-M2C-SYNC` | gate | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:PH-M2C-UI` | gate | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:PH-QA-FLOW` | gate | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:QA-GATE` | lane | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:QA-UI` | lane | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:QA-VERBS` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:SPLIT-PUBLIC-HISTORY` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:SPLIT-PUBLISH` | lane | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `plangraph:UI-TABLE` | lane | plangraph-L1-main | build-L1 | yes | INDEPENDENT |
| `plangraph:UI-WRITES` | lane | plangraph-L1-main | build-L1 | no | INDEPENDENT |
| `routing:REFERRAL-DOCS` | lane | autoos-L1-main | workst-L1-autoos | yes | INDEPENDENT |
| `server:ADOPT-400LOC-CHAPTARR` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-COMICS-SUPPORT` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-CUT-AUDIO-SOULSEEK` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-CUT-AUDIO-USENET-DE` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-CUT-COMICS-MANGA` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-CUT-EBOOK` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-CUT-IMPORT-AUDIO` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-CUT-IMPORT-EBOOK` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-CUT-MAGAZINES` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-CUT-RECONCILE` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-DB-BACKUP` | lane | chaptarr-L1 | server-L1-main | yes | - |
| `server:CH-GAP-ABS-NOTIFY` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-GAP-FAILED-HANDLING` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-GAP-MAGAZINES` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-GAP-RECLASSIFY` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-GAP-SEED-IMPORT` | lane | chaptarr-L1 | server-L1-main | yes | - |
| `server:CH-INDEXERS` | lane | chaptarr-L1 | server-L1-main | yes | - |
| `server:CH-SCOPE-COMICS` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-SEARCH-SERIES` | lane | chaptarr-L1 | server-L1-main | yes | - |
| `server:CH-SEED-APPLY-RC` | lane | chaptarr-L1 | server-L1-main | yes | - |
| `server:CH-SEED-COUNT-STABLE` | lane | chaptarr-L1 | server-L1-main | yes | - |
| `server:CH-SEED-COVERAGE` | lane | chaptarr-L1 | server-L1-main | yes | - |
| `server:CH-SEED-EXPLICIT-TYPE` | lane | chaptarr-L1 | server-L1-main | yes | - |
| `server:CH-TABLE` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-UP-77` | lane | chaptarr-L1 | server-L1-main | yes | - |
| `server:CH-UP-ABRLIST` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-UP-MULTILANG` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:CH-UP-TABLE` | lane | chaptarr-L1 | server-L1-main | no | - |
| `server:D1282-DOCKHAND-BACKUP` | lane | - | server-L1-main | yes | - |
| `server:D1282-DOCKHAND-UPDATE` | lane | - | server-L1-main | no | - |
| `server:D1282-GRAPHIFY-REBUILD` | lane | - | server-L1-main | no | - |
| `server:D1282-HAWSER-CHECK` | lane | - | server-L1-main | no | - |
| `server:D1282-INVEST-SCHED-BEFORE` | lane | - | server-L1-main | yes | - |
| `server:D1282-INVEST-SCHED-OFF` | lane | - | server-L1-main | no | - |
| `server:D1282-INVEST-SCHED-REENABLE` | lane | - | server-L1-main | no | - |
| `server:D1282-KUMA-READ-PATH` | lane | - | server-L1-main | yes | - |
| `server:D1282-NAVIDROME-ROTATE-CODE` | lane | - | server-L1-main | yes | - |
| `server:D1282-NAVIDROME-ROTATE-RUN` | lane | - | server-L1-main | no | - |
| `server:D1282-OWUI-AGY-ROW` | lane | - | server-L1-main | no | - |
| `server:D1282-OWUI-AUDIO-COMBOS` | lane | - | server-L1-main | no | - |
| `server:D1282-OWUI-NATIVE-FC` | lane | - | server-L1-main | no | - |
| `server:D1282-OWUI-OIDC-CLIENT` | lane | - | server-L1-main | no | - |
| `server:D1282-OWUI-OIDC-ENV` | lane | - | server-L1-main | no | - |
| `server:D1282-OWUI-OIDC-HIDEFORM` | lane | - | server-L1-main | no | - |
| `server:D1282-OWUI-PROBE` | lane | - | server-L1-main | yes | - |
| `server:D1282-OWUI-READALOUD-SRC` | lane | - | server-L1-main | yes | - |
| `server:D1282-OWUI-SYNC-TOOLIDS` | lane | - | server-L1-main | yes | - |
| `server:D1282-OWUI-TTS-VOICES` | lane | - | server-L1-main | no | - |
| `server:D1282-OWUI-UPD-BACKUP` | lane | - | server-L1-main | yes | - |
| `server:D1282-OWUI-UPD-COMPOSE` | lane | - | server-L1-main | no | - |
| `server:D1282-OWUI-UPD-DEPLOY` | lane | - | server-L1-main | no | - |
| `server:D1282-OWUI-WEBSEARCH-FIX` | lane | - | server-L1-main | no | - |
| `server:D1282-OWUI-WIKI-TOOL` | lane | - | server-L1-main | yes | - |
| `server:D1282-REPO-CODING-FF` | lane | - | server-L1-main | yes | - |
| `server:D1282-REPO-PROX-STATE` | lane | - | server-L1-main | yes | - |
| `server:D1282-SAB-HEALTHCHECK` | lane | - | server-L1-main | no | - |
| `server:D1282-SAB-INCOMPLETE-NVME` | lane | - | server-L1-main | no | - |
| `server:D1282-SAB-LOG-PURGE` | lane | - | server-L1-main | no | - |
| `server:D1282-SAB-LOG-REDACT` | lane | - | server-L1-main | yes | - |
| `server:D1282-SAB-NFS-CONFIRM` | lane | - | server-L1-main | yes | - |
| `server:D1282-SAB-REGRAB-LOOP` | lane | - | server-L1-main | no | - |
| `server:D1282-TDARR-CONFIRM` | lane | - | server-L1-main | yes | - |
| `server:D1282-TDARR-FULL-RUN` | lane | - | server-L1-main | no | - |
| `server:D1282-TDARR-KEEP-FLOW` | lane | - | server-L1-main | no | - |
| `server:D1282-TDARR-METRIC` | lane | - | server-L1-main | no | - |
| `server:D1282-TDARR-SAMPLE` | lane | - | server-L1-main | no | - |
| `server:D1282-TDARR-VERIFY-PB` | lane | - | server-L1-main | no | - |
| `server:D1282-TTS-DIARISE` | lane | - | server-L1-main | no | - |
| `server:D1282-TTS-MODEL-LIST` | lane | - | server-L1-main | yes | - |
| `server:D1282-TTS-RESEARCH` | lane | - | server-L1-main | yes | - |
| `server:D1282-TTS-SPEAKR-WIRE` | lane | - | server-L1-main | no | - |
| `server:D1282-TTS-STT-FALLBACK` | lane | - | server-L1-main | no | - |
| `server:MT-PBS-HYGIENE` | goal | - | server-L1-main | yes | - |
| `server:PH-D1282` | goal | - | server-L1-main | no | - |
| `server:PL-ACCOUNTS-V02` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:PL-P10-BUILD-LINES` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:PL-P10-PUBLISH` | lane | server-L2-maint | server-L1-main | no | - |
| `server:PL-P3-HIDS` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:PL-P7-DOCS-IA` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:PL-P9-CLAMAV` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-005` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-006` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-008` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-010` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-013` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-015` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-021` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-024` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-025` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-026` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-027` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-028` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-029` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-030` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-031` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-032` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-033` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-035` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-036` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-037` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-038` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-039` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-040` | lane | server-L2-maint | server-L1-main | yes | - |
| `server:T-041` | lane | server-L2-maint | server-L1-main | yes | - |
