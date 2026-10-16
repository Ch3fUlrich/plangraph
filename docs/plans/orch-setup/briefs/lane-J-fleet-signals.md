# Lane J: fleet signals (plangraph-L2-fc-signals)

STANDING RULES: `docs/plans/orch-setup/briefs/COMMON.md` (binding, read first). Design sections: 5 (env keys), 12.1.
Items, in order: `plangraph:HOST-SAMPLES-API`, `plangraph:HOST-AGENT`, then `plangraph:AGENT-SESSION-SIGNALS` and
`plangraph:GATEWAY-AVAILABILITY-SIGNALS` (these two may run in parallel).
- HOST-SAMPLES-API starts once SU-DOCS is merged. Base it on main `b3d71096` or later (the keeper runs on Postgres).
- HOST-AGENT waits for lane A (SU-CORE, SU-ENV).
- AGENT-SESSION-SIGNALS waits for lane C's SU-LAUNCH.

## READ
- Keeper Postgres backend (`PG_KEEPER_BACKEND=postgres`, schema `keeper`, runtime role `keeper_rt` without DDL) and its
  migration path; `src/plangraph/keeper/api.py` route style; the token checks in `store_tokens.py`.
- `rust/plangraph-cli` (lane A): env loader, manifest, merge engine. The host-agent is a new module plus a `host-agent`
  subcommand.
- AutoOS `context_guard.py` state (`~/.local/state/autoos/ctx-guard/<sid>.json`, field `ctx`) and the launch records
  (SU-LAUNCH).
- Claude Code statusline docs (`context_window.used_percentage`, `context_window_size`) and hook input (`transcript_path`).
  Check them with context7.
- Workstation reference commands (routing, 2026-10-16). Use them only to check the agent's values, never as the
  implementation:
  - `Get-CimInstance Win32_OperatingSystem`;
  - `nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.free,memory.total --format=csv,noheader`;
  - `free` / `nproc` in WSL.

## WRITE SCOPE
- Keeper: a new `host_samples` module, a migration, routes `POST /hosts/<id>/samples` and `GET /hosts`, `GET /hosts/<id>`.
  Tests `tests/test_host_samples*.py`.
- Rust: `rust/plangraph-cli/src/host_agent/**`, manifest component `host-agent` (user service), tests.
- `setup/env.schema.toml`: add PG_HOST_ID, PG_HOST_TOKEN_FILE, PG_HOST_SAMPLE_S. `docs/SETUP.md`: a host-agent section.

## Spec
Follow design 12.1 exactly. Key points:
- **Ingest:**
  - a per-host token scoped to ingest only;
  - server-clock `received_at`;
  - `seq` and `agent_started_at`;
  - `stale` after 3 missed intervals and `down` after 10, computed server-side.
- **Storage:**
  - range partitions;
  - raw rows 14 days, 1-minute rollups 90 days, 1-hour rollups 2 years;
  - maintained by the keeper's own loop (no cron, D-1377).
- **Agent:**
  - crates `sysinfo` (reuse one `System`, use `refresh_specifics`) and `nvml-wrapper`, with an `nvidia-smi` subprocess
    fallback;
  - sample every 5-10 s; push mean + max every `PG_HOST_SAMPLE_S`;
  - WSL2 reports as its own node with its `.wslconfig` memory cap;
  - CPU utilisation, not load average;
  - memory: Linux `MemAvailable`, Windows Available Bytes.
- **Service:**
  - Linux: systemd `--user` with linger, `NoNewPrivileges=yes`;
  - Windows: a logon-start entry for the current user;
  - installed by `plangraph setup apply` under the same idempotency contract (section 4); `setup check` reports drift.
- **Sessions:**
  - Claude context = the last assistant usage (input + cache_read + cache_creation), read-only from the transcript;
  - the window size comes from a per-model table, overridden by the statusline or ctx-guard state;
  - opencode via `opencode serve` (`/session`, `/session/:id/message`), else a read-only open of its store after a version
    check;
  - qoder/agy: count + age.
- **Gateway:**
  - read OmniRoute's own cooldown/health state (spike first: find the endpoint);
  - per provider/model: requests, 429s, `Retry-After`, cooldown and spend-cap state with its reset time;
  - no paid LLM calls.
- **Payload test:** no paths, args, env, prompts or transcript text ever leave the host.

Checks:
- keeper suite green on SQLite + Postgres CI jobs;
- the agent on Linux, Windows and WSL2 posts to a test keeper;
- 2nd setup run skipped;
- agent CPU < 1 % (measured, in the record);
- Claude context within 2 % of a live statusline value;
- an injected 429 visible within 2 samples.

## Return
DONE per item: branch, head sha, check output tail, writer model(s), seat verdicts.
