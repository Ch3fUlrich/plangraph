# L2 brief: plangraph:BUS-PEER-DISCOVERY-ADOPT (HIGH p8, D-2670/D-2671) · parent build-L1-bus
Load skill unattended-orchestration and follow its L2 (orch) rules. You orchestrate and never write code yourself (R-coord-14). L3 writers and seats start only via the autoos-agent spawn (R-coord-09).

You never merge or push main, and run no crons. Keep a card <= 40 lines at l2-cards/discovery-adopt/card.md in /home/s/code/plangraph-worktrees/build-L1-bus. Send "RECYCLE-READY <card>" before 270k context.

## Goal
routing bin/fleet-poll.py and AutoOS tools/fleet_bus.py take their peer endpoints from the keeper registry, not hardcoded values:
- source: keeper GET hosts (`/v1/hosts`, auth bearer; fields host, endpoint, key_id, health, last_seen), or tailscale via plangraph fleet_bus.discovery;
- this replaces routing's hardcoded BASE_URL (a site IP literal, fleet-poll.py:30) and AutoOS's DEFAULT_BIND assumption (fleet_bus.py:59);
- per-host key files replace the shared ~/.config/autoos/fleet-relay-secret on both ends.

## Done means
- No site IP literal is left in either repo. Evidence: the grep command and its output, for 100.x tailnet addresses and any other literal host IP outside docs/examples marked placeholder.
- Per-host key files work on both ends (key file path per host from an inventory; never print key contents).
- Tests are green in both repos (each repo's own CI/prepush). Each change has a red test first.
- 2 review seats per repo PR, of distinct families, both differing from every writer family (D-2511). Pick via seats_for / D-2449 order: qwen qodercli > opencode free deepseek-v4.1-flash-free / glm-5.3-flash-free; Haiku is allowed as 1 seat. Seat prompts get item + diff + artifacts only, NO-tools header, no rulings (D-302). Tool-less seats never via agy -p (D-2688).
- MERGE-READY <repo> <branch> <sha> <CI/prepush record> <seat .out paths + sha256>, sent to build-L1-bus. I merge (routing: send to routing session as well, it owns that repo's main).

## Lanes (already cut from origin/main)
- routing: /home/s/code/routing-worktrees/bus-discovery-adopt, branch lane/bus-discovery-adopt
- AutoOS: /home/s/code/AutoOS-worktrees/bus-discovery-adopt, branch lane/bus-discovery-adopt
Keeper side (reference only, do not edit): plangraph phase/rt-p1-bus, src/plangraph/keeper/api.py `_read` name=="hosts", src/plangraph/fleet_bus/discovery.py Registry/Host. The phase is not on main/live yet. The clients must:
- fail closed with a clear error when the keeper hosts read is unavailable;
- offer an explicit --base/env override for transition, and default to discovery, never to an IP.

## Writers (D-2683/D-2684, never Claude)
Order: agy/gemini-3.8-flash > mimo/mimo-v2.6-pro > openrouter/free + opencode free (reset ~10-26 01Z) > mimo/mimo-v2.6-flash. Record each skip with its reason.
- One writer per repo; they can run in parallel.
- Memory floor D-2706: no spawn while MemAvailable < 2000 MB. Max 4 concurrent writers on coding.vm (I am using 1).
- max_tokens 128000 (D-2694). finish=length with empty output = retry at the leg ceiling, never skip.
- Commit trailers: `Plan-Item: plangraph:BUS-PEER-DISCOVERY-ADOPT` and `Writer: <model> <run id>`.

## Fences
- No .github edits.
- No secrets read or printed (names only).
- No host/network changes.
- Never write ~/.omniroute.
- Redact IPs in seat prompts.

Report, then stop (D-2442).
