# Orchestration setup: one mechanical install of the agent system (design + plan)

plangraph-L1-orch-setup, 2026-10-16. Status: DRAFT for the routing judge pass, then operator approval.
Base: [2026-10-15-rethink-setup-design.md](2026-10-15-rethink-setup-design.md) (D-1285, approved) and
[operator-rounds.md](operator-rounds.md) (verbatim). This file adds the setup layer and three UI items. It never overrides the
base; where the two differ, the base wins and the difference is listed in section 11.

Goal (base, section 0): many agents from different providers work long-running and unattended on many projects, without
babysitting, at high quality and speed and low cost, with one overview that shows everything.
Goal of this file: **one mechanical, replicable setup** of that system that no agent can misread. plangraph is the single source
of it; AutoOS (machine installer) runs it at a pinned tag and copies no logic.

Rounds 11-12 of the base came after its approval: round 11 keeps the M1 speed target (already in base section 9); round 12
stopped the supervising session. Neither changes this design.

## 1. Decisions taken in this round (operator, 2026-10-16)

| # | Decision | Source |
|---|---|---|
| S1 | Hooks, launcher and installer are **Rust** in plangraph (`rust/` workspace), one binary `plangraph`. Python hooks in AutoOS stay as the reference until the Rust port replaces them. Budget: a no-op hook < 10 ms | operator via workst-L1-autoos; router order 3 |
| S2 | plangraph becomes **public** with no private content; private data lives in `plangraph-data`. Cutover = a fresh public repo built by `tools/split/make_public.py` (scrub, deny, gitleaks); today's private repo is kept as a private archive. This is the existing SPLIT track | AskUserQuestion |
| S3 | AutoOS gets the binary as a **pinned GitHub release + sha256** from the public repo | AskUserQuestion |
| S4 | plangraph's setup **merges** hook entries into `~/.claude/settings.json`; AutoOS amends ADR 0003 (AutoOS itself still writes nothing; the plangraph component it calls does) | AskUserQuestion |
| S5 | bash_guard and context_guard are **ported to Rust later** into the same home (plangraph) and wired by the same setup | AskUserQuestion |
| S6 | ONE env file, user level: `~/.config/plangraph/plangraph.env` (override `PLANGRAPH_ENV_FILE`); repo ships `.env.example` | AskUserQuestion |
| S7 | Each fleet repo: `CLAUDE.md` is exactly `@AGENTS.md`; rules live only in `AGENTS.md`; a lint fails on anything else | AskUserQuestion |
| S8 | One launcher `plangraph launch`: pre-assigned session id, launch record written **before** start, routing's `fleet-launch` becomes a thin wrapper | AskUserQuestion |
| S9 | Keeper identity: the user-scope MCP entry has no token; `plangraph launch` sets `PG_TOKEN_FILE`: an L1/planner gets its own token, a child session gets a **run-bound token minted by its parent** | AskUserQuestion |
| S10 | `Plan-Item` trailer: **auto-fix** commit-msg hook + `plangraph trailers check` in CI | AskUserQuestion |
| S11 | Heartbeat cadence lives in the env file plus an agent rule; no network I/O in hooks; fix the keeper so its lease config keys apply | AskUserQuestion |
| S12 | Skills linking logic **moves to plangraph** (`plangraph setup skills`); AutoOS's `Install-AutoOSAgentSkills` / `link_skill_dirs` become thin callers | AskUserQuestion |
| S13 | Docker setup is the last item: after the **whole rethink** (`routing:PH-RETHINK`) | AskUserQuestion |
| S14 | Graph history tab: view any past point and diff two points; "go back" opens a correction proposal through the planning-mode UI; DONE is never rewritten | AskUserQuestion |
| S15 | Router order 2026-10-16 (items 1-7): plan base in `docs/plans/`, Rust code, SvelteKit site, plangraph wires OmniRoute per project, one project = one repo, repo hooks only where needed, all other automation through the plangraph MCP server | routing, confirmed by the operator |
| S16 | The MCP entry runs a **Rust `plangraph mcp`** stdio server in the same binary: the existing tool set forwarded 1:1 to the keeper HTTP API, contract-tested against the Python MCP. No Python on client machines | AskUserQuestion |
| S17 | Lane models: writer **gemini-3.8-flash via agy**; seats **qwen3.8-flash (qodercli)** + **opencode/muse-spark-1.3-free**; a rate-limited leg falls back to mimo-v2.6-flash, then deepseek. Seat family != writer family always. agy gets code only, never plan data | AskUserQuestion |
| S18 | Public repo CI on **GitHub-hosted** Linux + Windows runners (tests + release builds); the workstation runner stays for private repos | AskUserQuestion |

Verified facts (this round): Claude Code 2.1.301 on Windows, `claude -p`, three temp repos: AGENTS.md alone -> loaded; CLAUDE.md =
`@AGENTS.md` -> loaded; CLAUDE.md with other text -> AGENTS.md NOT loaded. So a non-import CLAUDE.md silently hides AGENTS.md;
S7 plus the lint prevent that. Docs: native AGENTS.md needs >= 2.1.277 and is skipped by some session types; the import works
everywhere and fires the `InstructionsLoaded` hook. The WSL check could not run (the WSL Claude login has expired).

## 2. What runs where (roles, not hostnames)

| Role | Runs | Installed by |
|---|---|---|
| control-plane host | keeper (Postgres), Temporal server + UI, plangraph web UI (SvelteKit), projector, reconciler | Server repo deployments (base section 10) |
| worker host(s), WSL2 Linux | Rust worker, harnesses (opencode, qodercli, agy, OpenHands), worktrees per attempt | AutoOS machine install + `plangraph setup apply` |
| operator workstation (Windows and each WSL distro separately) | attended Claude sessions: planner, judge, expert; today also the fleet L1/L2 sessions | AutoOS + `plangraph setup apply` per OS environment |
| gateway | OmniRoute (window-class combos, failover); its server config stays in AutoOS | AutoOS |
| phone / browser | plangraph page: Plan, Runs, Inbox, Costs, Knowledge, History; push questions | none (web) |

Setup layers (one owner each):

| Layer | Command | Places |
|---|---|---|
| machine | AutoOS catalog component `plangraph-setup` | downloads the pinned `plangraph` release, verifies sha256, runs `plangraph setup apply`, detects with `plangraph setup check` |
| user environment | `plangraph setup apply` / `check` | binary, env file, Claude Code hooks, plangraph MCP entry, skills links, harness client config pointing at OmniRoute, state dirs |
| project repo | `plangraph setup repo <path>` | CLAUDE.md lint, commit-msg hook, project hooks only if that repo's manifest asks for them |
| session | `plangraph launch` | name, session id, parent, launch record, `FLEET_SESSION_NAME`, `PG_TOKEN_FILE` |

One project = one repository. Anything a repo does not need locally is served by the plangraph MCP server (wake-ups, messages,
questions, heartbeats), not by hooks.

## 3. How agents wake, talk and heartbeat

Today (interim, persistent fleet sessions):
- **Report:** a child's `Stop` hook (`plangraph hook report`) finds the first status line (DONE, MERGE-READY, BLOCKED, FAILED,
  RECYCLE-READY, STOP) in the last assistant turn and appends one redacted, capped row to the parent's inbox file.
- **Deliver:** the parent's `PostToolUse` / `UserPromptSubmit` hook (`plangraph hook inject`) reads its inbox from a byte cursor and
  injects new rows as untrusted context (20 rows / 4096 chars max). At least once; dedupe per session.
- **Wake:** an idle parent makes no tool calls, so it sees rows at its next prompt; an idle session is still woken by a
  cross-session message (`crossSessionInbound: accept`).
- **Heartbeat:** the holder of a keeper claim calls MCP `heartbeat` every `PG_HEARTBEAT_S` (default 900 s); the lease is
  `PG_LEASE_S` (default 7200 s). The keeper reaper frees expired claims.
- **Identity:** the session name and parent come from the launch record that `plangraph launch` writes before start.

Target (base sections 2, 4, 6):
- **Workers** hold no keeper token and use no hooks. Temporal owns every clock: activity heartbeat 30 s, timeout 2 min, retries,
  durable waits. Status is state (events); nobody sends status messages.
- **Questions** from a worker or a session go to the keeper Inbox, then to the phone as an interactive push (RT-P3-ASK-PUSH).
  An answer becomes a Temporal signal for a worker, or is delivered to the asking Claude Code session through the plangraph MCP
  server.
- **Planner wake** uses the same MCP path: a channel push if RT-P3-PHONE validates it, else an inbox long-poll (PG-INBOX) and the
  inject hook.
- **Retirement:** once the MCP path is proven, the report/inject hooks retire. Hooks stay only where they are local and cheap
  (commit-msg, guards).

Hook inventory (all one binary; command = absolute path of the installed binary, so no PATH edit and no checkout path):

| Hook | Event | Placed in | Budget | Status |
|---|---|---|---|---|
| `plangraph hook report` | Stop | user settings | < 10 ms no-op | port of AutoOS `fleet_report.py` (merged in AutoOS PR #110) |
| `plangraph hook inject` | PostToolUse, UserPromptSubmit | user settings | < 10 ms no-op | port of AutoOS `fleet_inject.py` |
| `plangraph hook commit-msg` | git commit-msg | each fleet repo | < 50 ms | new |
| `plangraph hook bash-guard` | PreToolUse (Bash) | user settings | < 10 ms no-op | later port of `bash_guard.py` |
| `plangraph hook context-guard` | PostToolUse | user settings | < 10 ms no-op | later port of `context_guard.py` |

## 4. The `plangraph` binary and its contract

Crate `rust/plangraph-cli` in the existing workspace, binary `plangraph`. Small dependency set (serde, serde_json, toml), no
async runtime in the hook paths. Every file <= 400 lines.

Commands:
- `setup apply [--dry-run]` and `setup check`, `setup repo <path>`, `setup skills`;
- `hook report|inject|commit-msg|bash-guard|context-guard`;
- `launch`;
- `trailers check <range>`;
- `lint claude-md [path]`;
- `env check`;
- `--version`.

What setup installs is data: `setup/manifest.toml` in the repo, embedded at build time. It lists hook entries, superseded
commands, MCP entry, skill targets per client, harness config targets and repo items.

Idempotency contract (every writer):
1. **Read, merge, write back.** Parse the target. If it cannot be parsed, refuse and write nothing. Never reorder or delete
   entries setup does not own. Keep unknown keys. Never replace a user's settings, PATH or profile wholesale.
2. **One line per action:** `<component> <target> installed|updated|skipped|drift|refused <reason>`. A second `apply` reports
   only `skipped`. `check` writes nothing and reports `drift` for every difference.
3. **Backup only on change:** `<file>.plangraph-backup-<UTC stamp>`, mode 0600, last 3 kept (only ours are pruned).
4. **Atomic writes:** temp file + fsync + rename, with a lock file next to the target.
5. **Exit codes:** 0 ok (or all skipped), 1 drift found by `check`, 2 usage error or refused, 3 partial failure.

Hook merge rule for `~/.claude/settings.json`:
- An entry with an identical command is `skipped`.
- An entry whose command matches a manifest `superseded` pattern (for example `python3 <any>/tools/hooks/fleet_report.py`) is
  replaced in place (`updated`).
- Otherwise a new matcher group is appended (`installed`).

Claude Code concatenates hook entries across settings levels and runs an identical handler once.

## 5. The env file

`~/.config/plangraph/plangraph.env`. Precedence: process env > file > built-in localhost defaults. Read by:
- the Rust binary;
- the Python keeper CLI/MCP and tools (one stdlib loader);
- the SvelteKit server (dynamic private env);
- Docker compose (`env_file:`).

Secrets never sit in it: tokens are file paths (`*_FILE` keys). `env check` refuses a key that looks like an inline secret.

| Key | Default | Used by |
|---|---|---|
| `PG_URL` | `http://127.0.0.1:8765` | CLI, MCP, launcher, UI server |
| `PG_TOKEN_FILE` | unset (the launcher sets it per session) | MCP, CLI |
| `PG_SNAPSHOT` | `~/.local/share/plangraph/plan.snapshot.json` | read fallback when the keeper is down |
| `PG_LEASE_S` / `PG_HEARTBEAT_S` | `7200` / `900` | claim holders (agent rule), keeper defaults |
| `PG_TLS_CA_FILE`, `PG_TLS_CERT_FILE`, `PG_TLS_KEY_FILE` | unset | mTLS keeper proxy (lane maps any existing names) |
| `OMNIROUTE_URL` | `http://127.0.0.1:20128` | harness client configs, worker (alias `AUTOOS_OMNIROUTE_URL` accepted) |
| `OMNIGRAPH_BASE_URL` | `http://127.0.0.1:8080` (lane confirms) | Knowledge tab, planners |
| `TEMPORAL_ADDRESS` / `TEMPORAL_UI_URL` | `127.0.0.1:7233` / `http://127.0.0.1:8233` | worker, deep links |
| `PLANGRAPH_UI_URL` | `http://127.0.0.1:5173` | push links, launcher hints |
| `FLEET_STATE_DIR` / `FLEET_LAUNCH_DIR` | `~/.local/state/fleet` / `~/.local/state/routing/launch` | hooks, launcher (old paths kept for compatibility) |
| `PLANGRAPH_SKILLS_SRC` | AutoOS `.agents/skills` until RT-P4-SPAWNER, then `plangraph/skills` | `setup skills` |

The same file works for Docker: the default compose uses host networking (as `deploy/keeper-proxy` already does), so the
localhost URLs stay valid unchanged.

## 6. Components (spec per item; done criteria in section 9)

- **Fleet hooks (Rust).** 1:1 behaviour of `fleet_report.py` / `fleet_inject.py` and `docs/fleet-status-hook.md`:
  - same inbox format and paths, append-only writes, symlink refusal (O_NOFOLLOW on Linux, a reparse-point check on Windows);
  - the same redaction, caps, cursor, `.seen` dedupe and untrusted framing, and silent failure (exit 0);
  - the 15 Python test cases ported, plus a latency test: p95 < 10 ms for a no-op over 200 runs on Windows and Linux.
- **Launcher.**
  - Arguments: `plangraph launch <name> --cwd D --model M --effort E --prompt-file F [--parent P] [--bg] [--mcp C] [--handoff H] [--respawn]`.
  - Generates a UUID, passes `--session-id`, and writes the record with the same JSON fields as routing's `fleet-launch`
    (`name, cwd, model, effort, mcp, prompt_file, handoff, parent, children, ts, session_id`) plus `env` (windows|wsl|linux), so
    `ctx-watchdog` keeps working.
  - Then sets `FLEET_SESSION_NAME` and `PG_TOKEN_FILE` and starts `claude` interactively (Remote Control name = session name) or
    with `--bg`.
  - If `--bg` refuses `--session-id`, it falls back to today's stdout parse.
  - The workstation `start-*.ps1` files become one-line calls; `fleet-launch` keeps its CLI and delegates.
- **Run tokens (HIGH tier).** For a child, the launcher calls keeper `mint_run_token` with the parent's token. It writes the
  token 0600 under `~/.local/state/plangraph/tokens/<name>.token`. The token expires with the lease and is revoked at the run's
  end. Never printed.
- **MCP server (Rust, S16).** `plangraph mcp` is a stdio JSON-RPC server.
  - It exposes the same tool names, schemas and error codes as `src/plangraph/keeper/mcp.py` (tool list at `mcp.py:40-119`) and
    forwards each call to the keeper HTTP API (`PG_URL`, bearer from `PG_TOKEN_FILE`, mTLS files if set).
  - A contract test runs the same calls through both servers against a test keeper and compares the results.
  - The Python MCP stays as reference until parity.
  - Later wake features (inbox long-poll, channel push, RT-P3) are added here.
- **MCP entry.** Registered with `claude mcp add-json --scope user plangraph` running `<abs path> mcp` (idempotent through
  `claude mcp get`). No token in the entry; values come from the env file. `setup check` runs `whoami` when `PG_TOKEN_FILE` is
  set. Supersedes `autoos:AO-PG-MCP-WIRE`.
- **Trailer hook.** `plangraph hook commit-msg`:
  - collects every `Plan-Item:` line and removes it from the body;
  - puts them at the head of the final trailer block (before `Co-Authored-By`), creating the block if absent;
  - checks the result with git's own trailer parsing (`git interpret-trailers --parse`);
  - refuses a malformed key; is idempotent.
  - Installed into the repo's hooks dir; if `core.hooksPath` is set it chains the existing hook and never replaces it.
  - `plangraph trailers check <range>` fails when a commit body holds `Plan-Item:` outside its trailer block. It runs in CI, so
    a `--no-verify` commit still fails before merge.
  - Fixture: the message of AutoOS `ba90f77a` must come out shaped like `05a376d3`.
- **Repo setup.** `setup repo` runs the CLAUDE.md lint and installs the trailer hook. A CLAUDE.md with rule text is `refused`
  with a hint: moving rules is a reviewed per-repo item, never automatic.
- **Skills.**
  - The per-client dir table (AutoOS AGENTS.md section 8) moves into the manifest: Claude Code `~/.claude/skills`, Antigravity
    `~/.gemini/config/skills`, gemini/qoder/qwen `~/.agents/skills` (qwen also `~/.qwen/skills`), codex `~/.codex/skills` if
    present, OpenHands `~/.agents/skills` + `~/.openhands/skills`.
  - Same rules as `link_skill_dirs`: absent -> link, ours or dangling -> repoint, anything else -> keep and report. Junctions on
    Windows, symlinks on Linux.
- **Harness client config.** On worker hosts, setup writes the OmniRoute client wiring of each harness that talks to the gateway
  (opencode provider block -> `OMNIROUTE_URL`, key by file path). The routing target per project/card stays keeper-side (base
  section 5). It replaces the opencode part of AutoOS `lib/agent_harness.py` once the worker adapters exist.
- **Keeper lease config (HIGH tier, keeper).** `default_lease`, `lease_warn_s`, `review_overdue_s` from keeper.json reach the
  Store. Today they are parsed but unused; the constants win.
- **Release.** A tag builds Linux x86_64 and Windows x86_64 binaries plus `SHA256SUMS` on GitHub-hosted runners (S18) as a
  GitHub release of the public repo.
- **AutoOS.**
  - Component `plangraph-setup` in `catalog/windows.json` and `catalog/linux.json`: pinned tag + sha256 in the catalog, nothing
    site-specific; second run `skipped`.
  - ADR 0003a records S4.
  - `docs/fleet-status-hook.md` points to plangraph; the Python hooks are marked reference only.
  - `Install-AutoOSAgentSkills` and `install_agent_skill_links` call `plangraph setup skills`.

## 7. Router order item 6, written down explicitly (existing items, no duplicates)

- **(a) Skills plan** = `plangraph:RT-P4-SKILLS-REWRITE`:
  - unattended-orchestration becomes a <= 150-line index plus role cards;
  - mechanical rules become code (this setup, the launcher, the trailer hook and the lints are such code);
  - skills live in `plangraph/skills`;
  - a CI lint rejects skills > 500 lines.
  - `autoos:T2-SKILL` (same content) should be cancelled as a duplicate (router ruling).
- **(b) routing into plangraph** = `plangraph:RT-P4-ROUTER-CODE` (generic router code), `plangraph:RT-P2-DECISIONS` +
  `plangraph:RT-P4-LEDGER-CUTOVER` (decisions/questions into keeper tables), then `routing:RT-P4-ARCHIVE`. The `fleet-launch`
  wrapper (S8) is the last change routing needs from this track.
- **(c) AutoOS parts into plangraph** = `plangraph:RT-P4-SPAWNER` (spawner substrate + skills, with history). Fleet hooks and
  guards go by this track. AutoOS then pins a plangraph tag (`autoos:RT-P4-INSTALLER`), and AutoOS deletions follow
  (`autoos:RT-P4-DELETIONS`). Already done: the Python fleet hooks merged in AutoOS (`autoos:FLEET-STATUS-HOOK`, interim).

## 8. New big-plan items (router order 7; phase P3, owner build-L1)

- **RT-P3-ASK-PUSH**: the page sends push notifications with interactive questions in the shape of Claude Code's question
  prompt:
  - 2-4 options, recommended first, single or multi select, free text "Other", optional preview;
  - the answer is routed to the asking Claude Code session (the question records the session name/id from the launch record;
    delivery through the plangraph MCP server), or to a Temporal signal for a worker;
  - auth path reviewed by 2 non-Claude seats.
- **RT-P3-PLAN-MODE**: planning mode through the same UI:
  - a plan change is a proposal (a batch diff: create, edit, deps, cancel) drawn as a graph diff;
  - the operator approves, edits or answers it per change;
  - applied changes are attributed events, and every task update shows in the graph live.
- **RT-P3-HISTORY**: a History tab with a time slider over the keeper event log:
  - renders the graph at any past point and diffs two points;
  - "go back" opens a correction proposal in plan mode (S14).

## 9. Plan items (track ORCH-SETUP unless noted; batch: `orch-setup/2026-10-16-orch-setup.yaml`)

| Key | Title | Deps | Done (check) |
|---|---|---|---|
| plangraph:SU-DOCS | Plan base + this design in docs/plans | - | the 2 base files byte-verified (body sha256), design merged via PR |
| plangraph:SU-CORE | `plangraph` binary core: env loader, manifest, merge engine, apply/check/dry-run, exit codes | SU-DOCS | contract tests (parse-refuse, skipped on 2nd run, drift, backup only on change, atomic write, lock) green on Linux + Windows CI |
| plangraph:SU-ENV | `.env.example`, `env check`, Python loader for keeper CLI/MCP/tools | SU-CORE | all keys of section 5 documented with defaults; precedence test; inline-secret refusal test; keeper tests green |
| plangraph:SU-LEASE-CFG | Keeper applies lease config keys (HIGH) | SU-DOCS, RT-P1-KEEPER-PG | test: keeper.json `default_lease` changes a claim's lease; suite green; 2 non-Claude seats |
| plangraph:SU-HOOK-FLEET | Rust fleet report/inject, 1:1 port | SU-CORE | 15 ported cases green; p95 no-op < 10 ms (Windows + Linux, 200 runs, numbers in the record); Windows reparse-point refusal test |
| plangraph:SU-HOOKS-WIRE | setup merges hook entries into settings.json | SU-HOOK-FLEET | fixture settings with foreign hooks unchanged; superseded Python entry replaced; 2nd run skipped; check reports drift |
| autoos:SU-ADR0003A | ADR 0003a + fleet-status-hook doc points to plangraph | plangraph:SU-HOOKS-WIRE | ADR merged; docs link check green |
| plangraph:SU-LAUNCH | `plangraph launch` with record before start | SU-CORE | record fields match fleet-launch; interactive + --bg; Windows + WSL; ps1 one-liners; watchdog fixture reads the record |
| routing:SU-FLEET-LAUNCH-WRAP | fleet-launch delegates to `plangraph launch` | plangraph:SU-LAUNCH | same CLI and record; dry-run output test |
| plangraph:SU-RUN-TOKENS | Launcher mints run-bound child tokens (HIGH) | SU-LAUNCH | child whoami = run principal; token 0600, never printed, revoked at end; 2 non-Claude seats |
| plangraph:SU-MCP-RS | Rust `plangraph mcp` stdio server, 1:1 with the Python MCP (HIGH) | SU-CORE, SU-ENV | contract test: same tool list, schemas, results and error codes as the Python MCP against a test keeper; 2 seats |
| plangraph:SU-MCP | User-scope plangraph MCP entry | SU-MCP-RS, SU-LAUNCH | 2nd run skipped; a launched session's whoami ok; supersedes autoos:AO-PG-MCP-WIRE |
| plangraph:SU-TRAILER | Auto-fix commit-msg hook + `trailers check` | SU-CORE | ba90f77a fixture fixed to the 05a376d3 shape; idempotent; malformed key refused; CI check fails a `--no-verify` fixture |
| plangraph:SU-REPO | `setup repo` + `lint claude-md` | SU-TRAILER | lint passes only on `@AGENTS.md`; chained hooksPath test; refused-with-hint test |
| plangraph:SU-AGENTSMD-PG | plangraph: AGENTS.md holds the rules, CLAUDE.md = `@AGENTS.md` | SU-REPO | lint in CI green; rules diff reviewed, no rule lost |
| autoos:SU-AGENTSMD-AO | AutoOS: same | plangraph:SU-REPO | same |
| server:SU-AGENTSMD-SRV | Server: same | plangraph:SU-REPO | same |
| plangraph:SU-SKILLS | `setup skills` with the per-client table | SU-CORE | rule tests (absent/ours/dangling/foreign), junction + symlink, 2nd run skipped |
| plangraph:SU-HARNESS-CFG | Harness client configs wired to OmniRoute | SU-ENV, plangraph:RT-P1-WORKER | opencode config merge test; drift check; no key value written |
| plangraph:SU-RELEASE | Tagged release binaries + SHA256SUMS | SU-CORE, plangraph:SPLIT-PUBLISH | a test tag publishes both binaries; sums verify |
| autoos:SU-CATALOG | AutoOS component `plangraph-setup` (pinned tag + sha256) | plangraph:SU-RELEASE, autoos:SU-ADR0003A | Windows + Linux: install, 2nd run skipped, detection = `setup check` |
| autoos:SU-SKILLS-CALLER | AutoOS skill installers call plangraph | plangraph:SU-SKILLS, autoos:SU-CATALOG | existing skill-link tests green through the binary |
| plangraph:SU-FRESH-CHECK | Fresh-environment check | all above except SU-HARNESS-CFG (see 11.8) | clean HOME on Linux + Windows: apply twice (2nd all skipped), check 0 drift, launched test pair delivers DONE, whoami ok, trailer fixed |
| plangraph:PH-ORCH-SETUP | gate: orchestration setup done | SU-FRESH-CHECK and all SU items above | all deps done |
| plangraph:SU-HOOK-BASHGUARD | Rust bash-guard, 1:1 | PH-ORCH-SETUP | ported tests green; < 10 ms; wired by setup |
| plangraph:SU-HOOK-CTXGUARD | Rust context-guard, 1:1 | PH-ORCH-SETUP | ported tests green; < 10 ms; wired by setup |
| autoos:SU-GUARDS-CALLER | OpenCode bash-guard plugin calls the binary | plangraph:SU-HOOK-BASHGUARD | plugin tests green |
| plangraph:PH-ORCH-GUARDS | gate: guards in Rust | the 3 guard items | all deps done |
| plangraph:RT-P3-ASK-PUSH | Interactive push questions routed to the asking session (HIGH) | RT-P3-PHONE, PG-INBOX | phone drill: question with options answered, the asking session receives it; 2 non-Claude seats |
| plangraph:RT-P3-PLAN-MODE | Planning mode through the UI | RT-P3-ASK-PUSH, RT-P2-DECISIONS | a proposed change approved in the UI lands as attributed events and shows in the graph |
| plangraph:RT-P3-HISTORY | History tab: time travel + diff | RT-P3-TABS, RT-P3-PLAN-MODE | render at any past seq equals a rebuilt snapshot (test); diff view; go-back opens a proposal |
| plangraph:SU-DOCKER | Docker setup with the same env file (LAST) | routing:PH-RETHINK, PH-ORCH-SETUP, PH-ORCH-GUARDS | compose up from the same env file; `setup check --docker` 0 drift; fresh-setup CI green in Docker |

Edits/cancels (router ops):
- `autoos:RT-P4-INSTALLER` gains deps `plangraph:PH-ORCH-SETUP`, `autoos:SU-CATALOG` and `plangraph:SU-HARNESS-CFG`.
- `routing:RT-P3` gains the three RT-P3 items.
- Cancel `autoos:AO-PG-MCP-WIRE` (superseded by SU-MCP).

Rulings left to the router:
- `autoos:AO-CTX-RECYCLE-ALLHOSTS` (launcher + guard now here);
- `autoos:T2-SKILL` (duplicate of RT-P4-SKILLS-REWRITE);
- `plangraph:API-PUSH` (fold into RT-P3-ASK-PUSH);
- `plangraph:CI-RUNS-ON-WORKST` (re-scope to private repos, S18).

## 10. Lanes (one L2 brief each, in `orch-setup/briefs/`; run by workst-L1-autoos)

| Lane | Items | Starts after |
|---|---|---|
| A core | SU-CORE, SU-ENV | approval + SU-DOCS merged |
| B hooks | SU-HOOK-FLEET, SU-HOOKS-WIRE, autoos:SU-ADR0003A | A's SU-CORE |
| C launch + MCP | SU-LAUNCH, SU-RUN-TOKENS, SU-MCP-RS, SU-MCP, routing:SU-FLEET-LAUNCH-WRAP | A |
| D repo | SU-TRAILER, SU-REPO, SU-AGENTSMD-PG/-AO/-SRV | A's SU-CORE |
| E skills+harness | SU-SKILLS, SU-HARNESS-CFG, autoos:SU-SKILLS-CALLER | A (HARNESS after RT-P1-WORKER, CALLER after SU-CATALOG) |
| F keeper lease | SU-LEASE-CFG | plangraph:RT-P1-KEEPER-PG on main (same keeper files; build-L1 coordination) |
| G release | SU-RELEASE, autoos:SU-CATALOG, SU-FRESH-CHECK | SPLIT-PUBLISH, lanes A-E |
| H guards | SU-HOOK-BASHGUARD, SU-HOOK-CTXGUARD, autoos:SU-GUARDS-CALLER | PH-ORCH-SETUP |
| I docker | SU-DOCKER | routing:PH-RETHINK |

Write sets are disjoint per lane (one module per subcommand under `rust/plangraph-cli/src/`). Lanes B-E can run in parallel
after A; F and the Python half of SU-ENV wait for the keeper's Postgres cutover. RT-P3 UI items are briefed by build-L1 with P3.

## 11. Differences from the base and open points

1. Base section 10 says the operator flips the existing plangraph repo public after PG-PUBLIC-READY. S2 makes it a fresh public
   repo from `make_public.py` (already the SPLIT track). The name `plangraph` must be freed first: rename the private repo to
   an archive name. This is an operator step inside `plangraph:SPLIT-PUBLISH`.
2. `docs/plans/` holds site strings (the two base files). It is outside the public allow-list (`tools/split/manifest.toml`)
   and stays private until scrubbed.
3. The seat conflict (mimo writer vs mimo seat) is resolved by S17.
4. **Latency budget.**
   - Definition: wall-clock from Claude Code starting the hook command to the process exit. The command is the binary's
     absolute path with no shell wrapper (no `bash -c`, no `python`). Measured over 200 runs, p95, on the workstation (Windows)
     and WSL2.
   - The first task of SU-HOOK-FLEET is a spike: an empty Rust `main` measured the same way. If the Windows floor already
     exceeds 10 ms, the lane reports the floor and the budget becomes floor + 5 ms (workst-L1-autoos records the ruling).
   - Gate: `setup apply` runs `plangraph hook bench` and wires the fleet hooks only when the budget holds; otherwise it reports
     `skipped budget <p95>`.
   - Until then the workstation keeps no fleet hooks (Python measured 165-335 ms per call).
5. **Root tokens are provisioned outside setup.** L1/planner token files are issued by the keeper admin/router (existing
   PG-TOKENS process) and only referenced by setup and the launcher.
   - Child run tokens are always minted, never provisioned.
   - A crashed child's token dies at lease expiry: run tokens get a TTL equal to the lease, and `launch --end` is only the
     early path.
   - SU-FRESH-CHECK uses a test keeper with a test principal.
6. **Roles.** Manifest components carry `roles = [workstation|worker]`. `setup apply --role` (env `PLANGRAPH_ROLE`, default
   `workstation`) selects them: hooks and the MCP entry for workstation, harness client config for worker, skills and env
   for both.
7. **Evidence across the public cutover.** DONE items keep their evidence (immutable). Their shas stay resolvable in the
   private archive repo, and the keeper's `repos` map points new evidence at the public repo after cutover. Lanes build in
   today's repo until SPLIT-PUBLISH; open lanes are then carried over as patches (SPLIT track).
8. **SU-HARNESS-CFG is outside the setup gate.** It waits for `plangraph:RT-P1-WORKER`, so it feeds
   `autoos:RT-P4-INSTALLER` directly and does not block PH-ORCH-SETUP.
9. **CI re-scope (S18).** `plangraph:CI-RUNS-ON-WORKST` must be re-scoped to the private repos (router ruling).
10. Left to the builder:
   - crate layout inside `rust/plangraph-cli`;
   - the ps1 one-liner form;
   - the backup stamp format;
   - the exact key regex (taken from the keeper);
   - whether `--bg` takes `--session-id` (test first).
