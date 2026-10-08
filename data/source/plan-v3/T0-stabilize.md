# T0 — Stabilize (phase 0, now)

Goal: no money leaks, no silent crashes, no stale actors, main can turn green.

| Lane | What | Owner | Depends | DONE (evidence) |
|---|---|---|---|---|
| T0-PAID | Set deepseek + meta-api legs `available: false` in the registry, re-render combos, gateway apply (drift 0), smoke every changed combo | AutoOS L1-main → L2 gateway lane | — | `combos.json` has no deepseek/meta leg; live drift 0; smoke 1 call/combo 200; usage report shows 0 new deepseek/meta calls for 24 h |
| T0-CONTAINER | Recreate `autoos-opencode` with `init: true`, pids_limit 32768, explicit `mem_limit` (8g) + memswap in compose (not `docker update`) | **OPERATOR** runs command below; router verifies | — | `docker inspect` → Init=true, PidsLimit=32768, Memory set; PID 1 = tini; zombies < 50 after 24 h |
| T0-HOLD | Stale Claude sessions (L1-main e74a2b09, L1-backlog, L1-routing, L2-general from 09-28) stay held; closed after the new L1-main is up and the operator agrees | routing-00 | T0-L1 | each session acked HOLD (done 11:3xZ); closed or relaunched |
| T0-L1 | Launch a fresh `autoos-L1-main` (Claude Sonnet, high effort) from the new handoff on an origin/main checkout | routing-00 | T0-SYNC | session up, ack names plan v3 + its first lane |
| T0-SYNC | Every coding.vm checkout/worktree used by orchestrators fast-forwards to origin/main; `d8b6e0ef import(infra) unscrubbed` is quarantined (branch renamed `quarantine/…`, never pushed); stash@{0} reviewed then dropped or kept as a branch | L1-main → L3 | — | `git log main..origin/main` empty on the main checkout; quarantine branch listed; no push of it (pre-push hook refuses `quarantine/*`) |
| T0-FREEZE | No merge to main until T3-CIGREEN lands, except T3-CIGREEN itself | L1-main (code: ready-check refuses while main CI is red) | — | ready-check test: red main → refuse; the CIGREEN lane flagged `fixes-main` → allowed |
| T0-KEYS | Key rotation: operator rotates every value (OS-31); agents then verify per provider with one probe call (names only), and update `api-keys.yml` hygiene (no duplicate keys, quoted values, strict-YAML parse passes) | **OPERATOR** rotates; L3 verifies | — | probe table all providers; `python3 -c 'import yaml; yaml.safe_load(open(...))'` passes; no duplicate names |
| T0-WSCLONE | Workstation agents work only in fleet clones (`~/fleet/AutoOS` WSL primary), never `C:\Users\…\Documents\Code\AutoOS`; the operator checkout is returned to `main` by the operator or left as is | workst-L1-autoos | — | opencode/Claude session cwds listed; none in the operator checkout |
| T0-ZEN1 | Remove the anonymous second "OpenCode Free" account from OmniRoute (Zen ToS bans multi-account use) | **OPERATOR** click; L3 verifies listing | — | providers page shows one opencode account |

## T0-CONTAINER command (host, coding.vm)
```
cd /home/s/code/AutoOS/configuration/docker/ai-stack && docker compose -p autoos-ai \
  --env-file ~/.config/autoos/ai-stack/stack.env -f compose.yml up -d --no-deps --force-recreate opencode
docker inspect -f '{{.HostConfig.Init}} {{.HostConfig.PidsLimit}} {{.HostConfig.Memory}}' autoos-opencode
```
Before that, the AutoOS lane T4-HARDEN updates `compose.yml` (pids 32768, mem_limit, memswap) so the recreate carries
them; if T4-HARDEN is not merged yet, recreate now with the current compose (init only) and once more after it lands.

## Why (evidence)
DeepSeek ≈ $14 in 3.5 days on a $19.99 balance, Meta failing 502/503/504; 7 pid-wall crashes because init was never
live; main red 13 pushes while takes continued; keys printed into transcripts on 09-29 (ground truth §3, §4).
