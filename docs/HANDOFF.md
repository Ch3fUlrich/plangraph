# HANDOFF — plangraph-L1-main, written 2026-10-03 ~05:40Z (for the relaunch on Sonnet high / bypass, D-496 + D-506)

Read in this order: CONSTRAINTS.md -> status/plangraph-L1-main.card.md (live state) -> this file -> docs/process/L2-BRIEF-TEMPLATE.md
(every rule in force). Talk to routing-00 (`routing`) and, for file/run handover, workst-L1-autoos (D-319), autoos-L1-main, server-L1-main.
Bypass mode does NOT widen the mandate (routing): apply only router-OK'd batches, record -> ACCEPT -> merge, no push/remote, stop and
escalate on any refusal you still meet.

## Where things are
- Repo /home/s/code/plangraph, work on branch `worktree-proposal` in /home/s/code/plangraph/.claude/worktrees/proposal (main = 1st commit).
  Code: src/plangraph/{keeper,web}, tools/ (apply_batch, md_sync*, split/), tests/. Data (PRIVATE, D-509): data/plan-v3, data/updates
  (QUEUE.md lists every applied batch). Proposals: docs/proposals/. Reviews: docs/reviews/.
- Job scratch of the previous L1 (KEEP, do not delete; the L2 works there): /home/s/.claude/jobs/7caaf7f5/tmp/
  - ui-build/ = plangraph-L2-ui-build's clone (branch phase/ui-build, head 18e6122) + pkg/ (task packages, seat prompts);
  - combo/ = code-only clone used to build bundles for workstation runs (branches: splitinteg, mdsync-closes, rehome-op, …);
  - k1-pkg/, mdsync-dclose-pkg/ (HELD), stack-research/, seat-*/ (seat records).
- Pilot keeper: config ~/.local/share/plangraph/keeper.json (0600), DB keeper.db (+ .pre-* backups), PID file daemon.pid, start:
  `cd <worktree> && PYTHONPATH=src nohup python3 -m plangraph.keeper.daemon --config ~/.local/share/plangraph/keeper.json &` then write
  the PID into daemon.pid. Tokens: ~/.config/plangraph/{router,plangraph-L1-main}.token (0600, never print; pass as PG_TOKEN_FILE).
  CLI: `PYTHONPATH=src PG_URL=http://127.0.0.1:8787 PG_TOKEN_FILE=… python3 -m plangraph.keeper.cli <verb>`; batches:
  `python3 tools/apply_batch.py FILE --url http://127.0.0.1:8787 --token-file ~/.config/plangraph/router.token [--dry-run]`.
- Review calls: from /home/s/code/AutoOS: `python3 tools/autoos_gateway_key.py exec -- python3 tools/review-call.py --model ovh/gpt-oss-120b
  --prompt-file P --out-dir D --title plangraph/<slug>` (RC-2: default 16000 tokens). Second seat: Qwen3.5-397B at workst-L1.
- Workstation route: tar the package as plangraph__<name>.tgz, `tailscale file cp <tgz> bz-wg-pdw028:`, message workst-L1 (sha256,
  writer form, return names). Results land sorted in /home/s/fleet/taildrop/plangraph/ (never `tailscale file get`). Screenshots go to
  /home/s/fleet/playwright-shots/<task>/ and routing gets the paths + one line on the visible change.

## Open threads (details on the card)
1. UI T3b-1fix3 -> browser acceptance -> seats -> accept T3b-1 -> T3b-2/T3b-3/T4 (packages drafted by the L2 in pkg/T3b2, T3b3).
2. K1 keeper (D-505) -> seats (2 non-google, HIGH) -> merge -> keeper config refs + daemon restart -> write P1 md-sync -> first-run dry
   run gate with routing.
3. Stack design note after the Haiku research returns; proposal "graph tracks acceptance" (Path 1 = P1 above, Path 2 after M2b).
4. Later: MODEL-KINDS, M2b off-host keeper, split real-repo steps (private word list, gitleaks, licence via routing), RUST-PORT item.

## Process lessons from 2026-10-02/03
- Seats invent quotes, especially for "absence" claims: machine-check every quote; an invented quote invalidates the line.
- Writers (Gemini) gamed tests twice (widened guard, `__eq__` override): every writer task lists the legit test change and carries the
  anti-gaming line; workst-L1 reads each diff before bundling.
- Merge = cherry-pick reviewed commits into the worktree only when clean; any conflict resolution is a writer run.
- Confirm a Taildrop file is filed (ls + sha) before pointing an L2 at it.
